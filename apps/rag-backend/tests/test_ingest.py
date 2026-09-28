"""Tests for knowledge-base ingestion: embedding, upsert, idempotency."""

from __future__ import annotations

import pytest


@pytest.fixture()
def patch_embed(app_module, monkeypatch):
    """Replace embed() with a deterministic fake and record its calls."""
    calls: list[str] = []

    async def fake_embed(text: str):
        calls.append(text)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(app_module, "embed", fake_embed)
    return calls


async def test_upsert_chunks_embeds_and_writes_points(app_module, fake_clients, patch_embed):
    _redis, qdrant = fake_clients
    chunks = [("chunk a", "01.md", 0), ("chunk b", "01.md", 1)]
    count = await app_module._upsert_chunks(chunks)
    assert count == 2
    assert len(patch_embed) == 2               # embedded each chunk
    assert len(qdrant.upserted) == 2           # wrote each point
    payload = qdrant.upserted[0].payload
    assert payload["text"] == "chunk a"
    assert payload["metadata"] == {"source": "01.md", "chunk": 0}


async def test_upsert_chunks_empty_writes_nothing(app_module, fake_clients, patch_embed):
    _redis, qdrant = fake_clients
    count = await app_module._upsert_chunks([])
    assert count == 0
    assert qdrant.upserted == []
    assert patch_embed == []


async def test_ingest_is_idempotent_same_ids(app_module, fake_clients, patch_embed):
    """Re-ingesting produces the same point IDs (overwrite, not duplicate)."""
    _redis, qdrant = fake_clients
    chunks = [("chunk a", "01.md", 0), ("chunk b", "01.md", 1)]
    await app_module._upsert_chunks(chunks)
    first_ids = [p.id for p in qdrant.upserted]

    qdrant.upserted.clear()
    await app_module._upsert_chunks(chunks)
    second_ids = [p.id for p in qdrant.upserted]

    assert first_ids == second_ids


async def test_ingest_knowledge_base_counts_real_corpus(app_module, fake_clients, patch_embed):
    _redis, _qdrant = fake_clients
    count = await app_module.ingest_knowledge_base()
    # Matches the number of chunks the loader produces from the bundled KB.
    expected = len(app_module.load_kb_documents(app_module.KB_DIR))
    assert count == expected > 0


async def test_ensure_collection_creates_when_missing(app_module, fake_clients):
    _redis, qdrant = fake_clients
    qdrant.collections = []  # nothing exists yet
    await app_module._ensure_collection()
    assert app_module.COLLECTION in qdrant.created


async def test_ensure_collection_noop_when_present(app_module, fake_clients):
    _redis, qdrant = fake_clients
    qdrant.collections = [app_module.COLLECTION]
    await app_module._ensure_collection()
    assert qdrant.created == []
