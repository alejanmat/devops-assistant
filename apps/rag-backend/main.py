"""Home Lab RAG backend — Ticino River (Pavia) fishing guide.

A minimal, dependency-light Retrieval-Augmented Generation API that ties
together the low-resource services described in the HLD:

  * LLM        -> llama-server (OpenAI-compatible /v1) on the compute cluster
  * Embeddings -> llama-server (--embedding) on the compute cluster
  * Vector DB  -> Qdrant on the storage cluster
  * Chat state -> Redis on the storage cluster

This instance is specialised as an expert assistant on **fishing techniques
for the Ticino River around Pavia, Italy**. On startup it loads the local
`knowledge_base/` documents, chunks and embeds them, and upserts them into the
Qdrant collection so `/chat` can answer grounded questions immediately.

All service endpoints are reachable across clusters over the Tailscale mesh via
the NodePorts declared in the infrastructure manifests. Everything is
configured through environment variables so the same image runs unchanged in
every environment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("rag-backend")

# --------------------------------------------------------------------------- #
# Configuration (all overridable via environment variables)
# --------------------------------------------------------------------------- #
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://llama-llm-nodeport.compute:8080")
EMBEDDING_BASE_URL = os.getenv(
    "EMBEDDING_BASE_URL", "http://llama-embedding-nodeport.compute:8080"
)
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant-nodeport.storage:6333")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis-nodeport.storage:6379/0")

COLLECTION = os.getenv("QDRANT_COLLECTION", "ticino_fishing")
# nomic-embed-text-v1.5 produces 768-dimensional vectors.
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
TOP_K = int(os.getenv("RAG_TOP_K", "4"))
HISTORY_TURNS = int(os.getenv("RAG_HISTORY_TURNS", "6"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "120"))

# Knowledge base: local documents auto-ingested on startup.
KB_DIR = pathlib.Path(os.getenv("KB_DIR", str(pathlib.Path(__file__).parent / "knowledge_base")))
KB_AUTO_INGEST = os.getenv("KB_AUTO_INGEST", "true").lower() in ("1", "true", "yes")
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "900"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "150"))
# Below this best-match cosine score, treat the question as out of scope.
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.35"))

# Domain-specialised system prompt: a Ticino/Pavia fishing expert.
SYSTEM_PROMPT = (
    "You are 'Ticino Angler', an expert fishing guide specialised in the "
    "Ticino River around Pavia, in Lombardy, Italy, and the surrounding Parco "
    "Lombardo della Valle del Ticino. You advise anglers on techniques, target "
    "species, tackle, bait, seasons, water conditions, access points, and local "
    "regulations for this river.\n"
    "Rules:\n"
    "1. Answer ONLY using the provided context passages from the knowledge base. "
    "Do not invent facts, spots, or figures that are not supported by the context.\n"
    "2. If the context does not cover the question, say you don't have that "
    "information for the Ticino at Pavia, and suggest what the angler could ask "
    "instead — do not guess.\n"
    "3. If the question is unrelated to fishing the Ticino near Pavia, politely "
    "explain that you only cover that topic.\n"
    "4. Be practical and concise. Use the species' Italian names where the "
    "context provides them (e.g. luccio, siluro, cavedano, aspio).\n"
    "5. When advice touches licences, closed seasons, sizes or protected zones, "
    "remind the angler to verify current local rules before fishing."
)


# --------------------------------------------------------------------------- #
# Clients (initialised on startup, shared across requests)
# --------------------------------------------------------------------------- #
class Clients:
    """Container for the long-lived async clients shared across requests.

    The instances are created once in :func:`lifespan` on application startup
    and closed on shutdown, avoiding per-request connection setup. Attributes:

    * ``http``   -- ``httpx.AsyncClient`` used to call the llama-server LLM and
      embedding endpoints.
    * ``qdrant`` -- ``AsyncQdrantClient`` for the vector store.
    * ``redis``  -- async Redis client holding per-session chat history.
    """

    http: httpx.AsyncClient
    qdrant: AsyncQdrantClient
    redis: "redis.Redis"


clients = Clients()


async def _ensure_collection() -> None:
    """Create the Qdrant collection on first boot if it does not exist."""
    existing = await clients.qdrant.get_collections()
    names = {c.name for c in existing.collections}
    if COLLECTION not in names:
        logger.info("Creating Qdrant collection %r (dim=%d)", COLLECTION, EMBEDDING_DIM)
        await clients.qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=qmodels.VectorParams(
                size=EMBEDDING_DIM, distance=qmodels.Distance.COSINE
            ),
        )


# --------------------------------------------------------------------------- #
# Knowledge-base loading and chunking
# --------------------------------------------------------------------------- #
def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries.

    Markdown paragraphs (blocks separated by a blank line) are greedily grouped
    until adding the next paragraph would exceed ``size`` characters. When a
    boundary is crossed, the tail of the previous chunk is carried over as up to
    ``overlap`` characters so semantic context is not lost between adjacent
    chunks. A single paragraph longer than ``size`` is hard-split into
    ``size``-character windows that also overlap by ``overlap``.

    Args:
        text: The full document text to split.
        size: Soft maximum size of each chunk, in characters.
        overlap: Number of trailing characters repeated at the start of the
            next chunk to preserve context across boundaries.

    Returns:
        A list of non-empty, stripped chunk strings in document order. Returns
        an empty list for blank input.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
            continue
        if current:
            chunks.append(current)
        if len(para) <= size:
            # Start the next chunk with an overlap tail from the previous one.
            tail = current[-overlap:] if current and overlap else ""
            current = f"{tail}\n\n{para}".strip() if tail else para
        else:
            # A single very long paragraph: hard-split it with overlap.
            start = 0
            while start < len(para):
                chunks.append(para[start : start + size])
                start += size - overlap
            current = ""
    if current:
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def _stable_point_id(source: str, index: int) -> str:
    """Return a deterministic Qdrant point UUID for a KB chunk.

    The ID is derived from ``"{source}#{index}"`` via SHA-1, so the same file
    and chunk position always map to the same point. Re-ingesting the knowledge
    base therefore overwrites existing points instead of creating duplicates,
    making startup ingestion and ``POST /reindex`` idempotent.

    Args:
        source: The knowledge-base source filename (e.g. ``"02-species.md"``).
        index: Zero-based position of the chunk within that file.

    Returns:
        A UUID string suitable for use as a Qdrant point ID.
    """
    digest = hashlib.sha1(f"{source}#{index}".encode()).hexdigest()
    return str(uuid.UUID(digest[:32]))


def load_kb_documents(directory: pathlib.Path) -> list[tuple[str, str, int]]:
    """Load and chunk every knowledge-base document in ``directory``.

    Only ``*.md`` and ``*.txt`` files are read; the index ``README.md`` is
    skipped because it describes the corpus rather than being content. Each file
    is chunked with :func:`chunk_text`.

    Args:
        directory: Path to the knowledge-base folder.

    Returns:
        A list of ``(chunk_text, source_filename, chunk_index)`` tuples in
        deterministic (sorted-filename, ascending-index) order. Returns an empty
        list if ``directory`` does not exist.
    """
    docs: list[tuple[str, str, int]] = []
    if not directory.is_dir():
        logger.warning("Knowledge-base directory %s not found; skipping.", directory)
        return docs
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        if path.name.lower() == "readme.md":
            continue  # index file, not knowledge content
        text = path.read_text(encoding="utf-8")
        for i, chunk in enumerate(chunk_text(text)):
            docs.append((chunk, path.name, i))
    return docs


async def _upsert_chunks(chunks: list[tuple[str, str, int]]) -> int:
    """Embed the given chunks and upsert them into Qdrant.

    Each chunk is embedded via :func:`embed` and stored with a deterministic ID
    from :func:`_stable_point_id`, plus a payload containing the raw ``text``
    and ``metadata`` (``source`` filename and ``chunk`` index).

    Args:
        chunks: ``(text, source, index)`` tuples, typically from
            :func:`load_kb_documents`.

    Returns:
        The number of points upserted (zero if ``chunks`` is empty).
    """
    points: list[qmodels.PointStruct] = []
    for text, source, index in chunks:
        vector = await embed(text)
        points.append(
            qmodels.PointStruct(
                id=_stable_point_id(source, index),
                vector=vector,
                payload={"text": text, "metadata": {"source": source, "chunk": index}},
            )
        )
    if points:
        await clients.qdrant.upsert(collection_name=COLLECTION, points=points)
    return len(points)


async def ingest_knowledge_base() -> int:
    """Load, chunk, embed and upsert the local knowledge base into Qdrant.

    Combines :func:`load_kb_documents` and :func:`_upsert_chunks`. Safe to call
    repeatedly thanks to deterministic point IDs.

    Returns:
        The number of chunks ingested.
    """
    chunks = load_kb_documents(KB_DIR)
    count = await _upsert_chunks(chunks)
    logger.info("Ingested %d knowledge-base chunks from %s", count, KB_DIR)
    return count


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Application lifespan: open shared clients and warm the knowledge base.

    On startup, creates the shared HTTP/Qdrant/Redis clients, ensures the Qdrant
    collection exists, and (unless ``KB_AUTO_INGEST`` is disabled) ingests the
    local knowledge base. Ingestion failures are logged but non-fatal so the API
    still starts if the embedding service is not ready yet — the corpus can be
    loaded later via ``POST /reindex``. On shutdown, all clients are closed.
    """
    clients.http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    clients.qdrant = AsyncQdrantClient(url=QDRANT_URL, timeout=REQUEST_TIMEOUT)
    clients.redis = redis.from_url(REDIS_URL, decode_responses=True)
    await _ensure_collection()
    if KB_AUTO_INGEST:
        try:
            await ingest_knowledge_base()
        except Exception as exc:  # noqa: BLE001
            # Don't block startup if the embedding server isn't ready yet;
            # the KB can be (re)loaded later via POST /reindex.
            logger.warning(
                "Knowledge-base auto-ingest failed (%s); "
                "call POST /reindex once the embedding service is up.", exc
            )
    logger.info("Ticino fishing RAG backend ready.")
    yield
    await clients.http.aclose()
    await clients.qdrant.close()
    await clients.redis.aclose()


app = FastAPI(
    title="Ticino River Fishing RAG Backend",
    version="1.1.0",
    summary="Expert Q&A on fishing techniques for the Ticino River at Pavia, Italy.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class IngestDocument(BaseModel):
    """A single document to embed and store in the vector database."""

    text: str = Field(..., min_length=1, description="Raw document text to embed.")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary metadata stored with the vector."
    )


class IngestRequest(BaseModel):
    """Request body for ``POST /ingest``: a batch of documents."""

    documents: list[IngestDocument]


class ChatRequest(BaseModel):
    """Request body for ``POST /chat``."""

    message: str = Field(..., min_length=1, description="The angler's question.")
    session_id: str | None = Field(
        default=None,
        description="Existing session ID to continue a conversation; a new one "
        "is generated when omitted.",
    )


class ChatResponse(BaseModel):
    """Response body for ``POST /chat``."""

    session_id: str = Field(..., description="Session ID to reuse for follow-ups.")
    answer: str = Field(..., description="The model's grounded answer.")
    sources: list[dict[str, Any]] = Field(
        ..., description="Retrieved passages (text, metadata, score) used as context."
    )


# --------------------------------------------------------------------------- #
# Core helpers
# --------------------------------------------------------------------------- #
async def embed(text: str) -> list[float]:
    """Return an embedding vector for ``text``.

    Calls the llama-server embedding endpoint (nomic-embed-text-v1.5) via its
    OpenAI-compatible ``/v1/embeddings`` route.

    Args:
        text: The text to embed.

    Returns:
        The embedding as a list of floats (768-dimensional by default).

    Raises:
        httpx.HTTPError: If the embedding service is unreachable or errors.
    """
    resp = await clients.http.post(
        f"{EMBEDDING_BASE_URL}/v1/embeddings",
        json={"input": text, "model": "nomic-embed-text-v1.5"},
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


async def generate(messages: list[dict[str, str]]) -> str:
    """Generate a chat completion from the LLM.

    Calls the llama-server LLM (Qwen2.5-1.5B-Instruct) via its
    OpenAI-compatible ``/v1/chat/completions`` route.

    Args:
        messages: OpenAI-style chat messages (role/content dicts).

    Returns:
        The assistant's reply text, stripped of surrounding whitespace.

    Raises:
        httpx.HTTPError: If the LLM service is unreachable or errors.
    """
    resp = await clients.http.post(
        f"{LLM_BASE_URL}/v1/chat/completions",
        json={
            "model": "qwen2.5-1.5b-instruct",
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 512,
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def retrieve(query: str) -> list[dict[str, Any]]:
    """Retrieve the most similar knowledge-base chunks for ``query``.

    Embeds the query with :func:`embed`, then performs a cosine similarity
    search over the Qdrant collection.

    Args:
        query: The user's question.

    Returns:
        Up to ``TOP_K`` hits, each a dict with ``text``, ``metadata`` and
        ``score`` keys, ordered by descending similarity.

    Raises:
        httpx.HTTPError: If embedding the query fails.
    """
    vector = await embed(query)
    hits = await clients.qdrant.search(
        collection_name=COLLECTION, query_vector=vector, limit=TOP_K
    )
    return [
        {"text": h.payload.get("text", ""), "metadata": h.payload.get("metadata", {}),
         "score": h.score}
        for h in hits
    ]


def _history_key(session_id: str) -> str:
    """Return the Redis key that stores a session's chat history."""
    return f"chat:{session_id}"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: always returns ``{"status": "ok"}`` if the process runs."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """Report reachability of every downstream dependency."""
    checks: dict[str, Any] = {}
    try:
        await clients.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
    try:
        await clients.qdrant.get_collections()
        checks["qdrant"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["qdrant"] = f"error: {exc}"
    healthy = all(v == "ok" for v in checks.values())
    return {"ready": healthy, "checks": checks}


@app.post("/ingest")
async def ingest(req: IngestRequest) -> dict[str, Any]:
    """Embed and upsert ad-hoc documents into the vector store.

    Unlike ``/reindex`` (which loads the on-disk knowledge base), this accepts
    documents in the request body and stores them under freshly generated random
    IDs. Useful for adding one-off notes without editing files.

    Returns:
        ``{"ingested": <number of documents stored>}``.
    """
    points: list[qmodels.PointStruct] = []
    for doc in req.documents:
        vector = await embed(doc.text)
        points.append(
            qmodels.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={"text": doc.text, "metadata": doc.metadata},
            )
        )
    await clients.qdrant.upsert(collection_name=COLLECTION, points=points)
    return {"ingested": len(points)}


@app.post("/reindex")
async def reindex() -> dict[str, Any]:
    """(Re)load the local knowledge_base/ directory into Qdrant.

    Idempotent: chunks use deterministic IDs, so re-running updates existing
    points rather than duplicating them.
    """
    try:
        count = await ingest_knowledge_base()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"embedding failed: {exc}") from exc
    return {"reindexed_chunks": count, "kb_dir": str(KB_DIR), "collection": COLLECTION}


@app.get("/sources")
async def sources() -> dict[str, Any]:
    """List the knowledge-base source files currently on disk."""
    files = (
        sorted(
            p.name
            for p in KB_DIR.iterdir()
            if p.suffix.lower() in (".md", ".txt") and p.name.lower() != "readme.md"
        )
        if KB_DIR.is_dir()
        else []
    )
    return {"kb_dir": str(KB_DIR), "files": files}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Answer a question, grounded in the retrieved knowledge base.

    Pipeline:

    1. Retrieve the top matching KB chunks for the question.
    2. Apply an out-of-scope guard: drop hits below ``MIN_SCORE`` so the small
       model is not fed weak context. If nothing clears the bar, an empty
       context is used and the system prompt steers the model to decline
       instead of hallucinating.
    3. Prepend the specialised system prompt and recent per-session history
       (from Redis), then call the LLM.
    4. Persist the new turn to Redis with a 7-day TTL.

    Args:
        req: The chat request (message and optional session_id).

    Returns:
        A :class:`ChatResponse` with the answer, the session ID to reuse, and
        the relevant source passages used as context.

    Raises:
        HTTPException: 502 if retrieval or generation fails upstream.
    """
    session_id = req.session_id or str(uuid.uuid4())
    key = _history_key(session_id)

    try:
        hits = await retrieve(req.message)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"retrieval failed: {exc}") from exc

    # Out-of-scope guard: if nothing relevant was retrieved, don't let the
    # small model hallucinate — answer from an empty context and let the
    # system prompt steer it to decline.
    relevant = [h for h in hits if h.get("score", 0.0) >= MIN_SCORE]
    if relevant:
        context = "\n\n".join(
            f"[{h['metadata'].get('source', '?')}] {h['text']}" for h in relevant
        )
    else:
        context = "(no relevant knowledge-base passages found for this question)"

    # Pull recent conversation history from Redis (stored as alternating lines).
    raw_history = await clients.redis.lrange(key, -2 * HISTORY_TURNS, -1)
    history: list[dict[str, str]] = []
    for line in raw_history:
        role, _, content = line.partition("\x1f")
        if role and content:
            history.append({"role": role, "content": content})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Context:\n{context}"},
        *history,
        {"role": "user", "content": req.message},
    ]

    try:
        answer = await generate(messages)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"generation failed: {exc}") from exc

    # Persist this turn (use unit-separator so content may contain colons/newlines).
    await clients.redis.rpush(
        key, f"user\x1f{req.message}", f"assistant\x1f{answer}"
    )
    await clients.redis.expire(key, 60 * 60 * 24 * 7)  # 7-day TTL

    return ChatResponse(session_id=session_id, answer=answer, sources=relevant)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
