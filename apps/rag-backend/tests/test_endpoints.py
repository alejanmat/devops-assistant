"""Tests for the HTTP endpoints and the /chat retrieval + guard pipeline."""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Simple synchronous endpoints via TestClient
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(app_module, fake_clients, monkeypatch):
    """A TestClient whose lifespan wires the in-memory fakes.

    The app's lifespan normally constructs real httpx/Qdrant/Redis clients and
    runs KB ingestion. We patch those so startup keeps the fakes from
    ``fake_clients`` and performs no network or embedding calls.
    """
    fake_redis, fake_qdrant = fake_clients

    monkeypatch.setattr(app_module, "KB_AUTO_INGEST", False)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", lambda *a, **k: _FakeHTTP())
    monkeypatch.setattr(
        app_module, "AsyncQdrantClient", lambda *a, **k: fake_qdrant
    )
    monkeypatch.setattr(app_module.redis, "from_url", lambda *a, **k: fake_redis)

    with TestClient(app_module.app) as c:
        yield c


class _FakeHTTP:
    async def aclose(self) -> None:  # pragma: no cover - trivial
        pass


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_reports_dependency_status(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"] == {"redis": "ok", "qdrant": "ok"}


def test_sources_lists_kb_files(client):
    resp = client.get("/sources")
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert "README.md" not in files
    assert "02-species.md" in files


# --------------------------------------------------------------------------- #
# /chat pipeline (async, downstream services mocked)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def mock_pipeline(app_module, fake_clients, monkeypatch):
    """Mock retrieve() and generate() and capture the prompt sent to the LLM."""
    captured: dict = {}

    async def fake_generate(messages):
        captured["messages"] = messages
        context_msg = next(m for m in messages if m["content"].startswith("Context:"))
        captured["context"] = context_msg["content"]
        return "OUT_OF_SCOPE" if "no relevant" in context_msg["content"] else "Use a light jig."

    monkeypatch.setattr(app_module, "generate", fake_generate)
    return captured


async def test_chat_grounded_answer_with_sources(app_module, fake_clients, mock_pipeline, monkeypatch):
    async def fake_retrieve(query):
        return [
            {"text": "Spinning for perch: 2-7g jig heads near structure.",
             "metadata": {"source": "03-techniques-spinning.md", "chunk": 1}, "score": 0.82},
            {"text": "Pike need a wire trace in the backwaters.",
             "metadata": {"source": "02-species.md", "chunk": 2}, "score": 0.71},
        ]

    monkeypatch.setattr(app_module, "retrieve", fake_retrieve)

    resp = await app_module.chat(app_module.ChatRequest(message="How to catch perch spinning?"))
    assert resp.answer == "Use a light jig."
    assert len(resp.sources) == 2
    assert resp.sources[0]["metadata"]["source"] == "03-techniques-spinning.md"

    # The specialised system prompt is present and context cites the source file.
    system_msg = mock_pipeline["messages"][0]
    assert system_msg["role"] == "system"
    assert "Ticino Angler" in system_msg["content"]
    assert "[03-techniques-spinning.md]" in mock_pipeline["context"]


async def test_chat_out_of_scope_guard_drops_low_scores(app_module, fake_clients, mock_pipeline, monkeypatch):
    async def fake_retrieve(query):
        # All hits below MIN_SCORE -> guard should discard them.
        return [{"text": "unrelated", "metadata": {"source": "x"}, "score": 0.05}]

    monkeypatch.setattr(app_module, "retrieve", fake_retrieve)

    resp = await app_module.chat(app_module.ChatRequest(message="weather in Tokyo?"))
    assert resp.answer == "OUT_OF_SCOPE"
    assert resp.sources == []
    assert "no relevant" in mock_pipeline["context"]


async def test_chat_persists_history_to_redis(app_module, fake_clients, mock_pipeline, monkeypatch):
    fake_redis, _qdrant = fake_clients

    async def fake_retrieve(query):
        return [{"text": "chub take dry flies under trees.",
                 "metadata": {"source": "05-techniques-fly-fishing.md", "chunk": 0}, "score": 0.9}]

    monkeypatch.setattr(app_module, "retrieve", fake_retrieve)

    resp = await app_module.chat(app_module.ChatRequest(message="fly fishing for chub?"))
    key = app_module._history_key(resp.session_id)
    assert key in fake_redis.store
    # One user turn + one assistant turn recorded.
    stored = fake_redis.store[key]
    assert any(line.startswith("user\x1f") for line in stored)
    assert any(line.startswith("assistant\x1f") for line in stored)
    # A TTL was set on the session.
    assert fake_redis.expiries.get(key) == 60 * 60 * 24 * 7


async def test_chat_reuses_provided_session_id(app_module, fake_clients, mock_pipeline, monkeypatch):
    async def fake_retrieve(query):
        return [{"text": "carp on method feeder.",
                 "metadata": {"source": "06-carp-catfish.md", "chunk": 0}, "score": 0.8}]

    monkeypatch.setattr(app_module, "retrieve", fake_retrieve)

    resp = await app_module.chat(
        app_module.ChatRequest(message="carp tips?", session_id="my-session-123")
    )
    assert resp.session_id == "my-session-123"


async def test_chat_upstream_failure_returns_502(app_module, fake_clients, monkeypatch):
    import httpx

    async def failing_retrieve(query):
        raise httpx.ConnectError("embedding server down")

    monkeypatch.setattr(app_module, "retrieve", failing_retrieve)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await app_module.chat(app_module.ChatRequest(message="anything"))
    assert exc.value.status_code == 502


# --------------------------------------------------------------------------- #
# retrieve() maps Qdrant hits correctly
# --------------------------------------------------------------------------- #
async def test_retrieve_maps_qdrant_hits(app_module, fake_clients, monkeypatch):
    _redis, qdrant = fake_clients

    async def fake_embed(text):
        return [0.0, 0.1]

    monkeypatch.setattr(app_module, "embed", fake_embed)
    qdrant.set_search_result([
        types.SimpleNamespace(
            payload={"text": "hello", "metadata": {"source": "01.md"}}, score=0.9
        ),
    ])

    hits = await app_module.retrieve("query")
    assert hits == [{"text": "hello", "metadata": {"source": "01.md"}, "score": 0.9}]
