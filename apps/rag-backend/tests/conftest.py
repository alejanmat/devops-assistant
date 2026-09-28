"""Shared pytest fixtures and import shims for the RAG backend tests.

The real ``qdrant-client`` pulls in ``numpy``, whose prebuilt wheels require CPU
SIMD levels not present on every target host (notably the legacy hardware this
project targets). To keep the unit tests fast and portable, we install a
lightweight stub of ``qdrant_client`` into ``sys.modules`` *before* ``main`` is
imported. The tests exercise application logic (chunking, IDs, routing, the
retrieval/guard pipeline) rather than Qdrant's client internals, so the stub is
sufficient and keeps the suite hermetic.
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_qdrant_stub() -> None:
    """Register a minimal ``qdrant_client`` stub if the real one is unavailable
    or undesirable for unit testing."""
    if "qdrant_client" in sys.modules:
        return

    qc = types.ModuleType("qdrant_client")

    class AsyncQdrantClient:  # noqa: D401 - simple stand-in
        """No-op async Qdrant client; real behaviour is injected in tests."""

        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            pass

    qc.AsyncQdrantClient = AsyncQdrantClient

    http_mod = types.ModuleType("qdrant_client.http")
    models_mod = types.ModuleType("qdrant_client.http.models")

    class _Struct:
        """Generic record object that stores its kwargs as attributes."""

        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class VectorParams(_Struct):
        pass

    class PointStruct(_Struct):
        pass

    class Distance:
        COSINE = "Cosine"

    models_mod.VectorParams = VectorParams
    models_mod.PointStruct = PointStruct
    models_mod.Distance = Distance
    http_mod.models = models_mod
    qc.http = http_mod

    sys.modules["qdrant_client"] = qc
    sys.modules["qdrant_client.http"] = http_mod
    sys.modules["qdrant_client.http.models"] = models_mod


_install_qdrant_stub()


@pytest.fixture()
def app_module():
    """Import (once) and return the application module with the stub in place."""
    import main  # noqa: WPS433 - imported lazily after the stub is installed

    return main


class FakeRedis:
    """In-memory async stand-in for the subset of Redis used by the app."""

    def __init__(self) -> None:
        self.store: dict[str, list[str]] = {}
        self.expiries: dict[str, int] = {}
        self.pinged = False

    async def ping(self) -> bool:
        self.pinged = True
        return True

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        data = self.store.get(key, [])
        # Emulate Redis inclusive end indexing.
        if end == -1:
            return data[start:]
        return data[start : end + 1]

    async def rpush(self, key: str, *values: str) -> int:
        self.store.setdefault(key, []).extend(values)
        return len(self.store[key])

    async def expire(self, key: str, ttl: int) -> bool:
        self.expiries[key] = ttl
        return True

    async def aclose(self) -> None:  # closed on app shutdown
        pass


class FakeQdrant:
    """In-memory async stand-in for the subset of Qdrant used by the app."""

    def __init__(self, existing_collections: list[str] | None = None) -> None:
        self.collections = list(existing_collections or [])
        self.upserted: list[object] = []
        self.created: list[str] = []
        self._search_result: list[object] = []

    async def get_collections(self):
        cols = [types.SimpleNamespace(name=n) for n in self.collections]
        return types.SimpleNamespace(collections=cols)

    async def create_collection(self, collection_name: str, **_kwargs) -> None:
        self.created.append(collection_name)
        self.collections.append(collection_name)

    async def upsert(self, collection_name: str, points) -> None:
        self.upserted.extend(points)

    def set_search_result(self, hits: list[object]) -> None:
        self._search_result = hits

    async def search(self, collection_name: str, query_vector, limit: int):
        return self._search_result[:limit]

    async def close(self) -> None:  # closed on app shutdown
        pass


@pytest.fixture()
def fake_clients(app_module):
    """Wire fake Redis/Qdrant/HTTP clients onto the app's shared ``clients``.

    Returns the (redis, qdrant) fakes so tests can assert on them.
    """
    fake_redis = FakeRedis()
    fake_qdrant = FakeQdrant(existing_collections=[])
    app_module.clients.redis = fake_redis
    app_module.clients.qdrant = fake_qdrant
    app_module.clients.http = None  # calls are monkeypatched in tests
    return fake_redis, fake_qdrant
