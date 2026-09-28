# home-lab-rag-monorepo

Low-Resource Multi-Cluster GitOps Home Lab RAG system.

A fully autonomous, privacy-focused Retrieval-Augmented Generation (RAG) system
optimized for legacy hardware (decade-old 2-core / 4-thread Intel i5 CPUs with
DDR3 memory, ~8 GB total RAM).

This instance is specialised as **"Ticino Angler"** — an expert assistant on
fishing techniques for the **Ticino River around Pavia, Italy**. The RAG
backend ships with a built-in knowledge base
(`apps/rag-backend/knowledge_base/`) covering species, spinning, float/feeder,
fly fishing, carp/catfish tactics, seasons, and local regulations. The
documents are auto-ingested into Qdrant on startup, and `/chat` answers are
grounded in them (with an out-of-scope guard so the small model declines rather
than hallucinates).

## Paradigm

Decoupled Multi-Cluster GitOps using standalone local ArgoCD instances. Each
cluster runs its own independent ArgoCD instance that syncs only its assigned
subdirectory of this monorepo — no cross-cluster control-plane dependencies.

```
                 SINGLE MONOREPO (this repo)
                          │
          ┌───────────────┴───────────────┐
          │ Git Sync                       │ Git Sync
          ▼                                ▼
  CLUSTER 1: COMPUTE (Legacy i5)   CLUSTER 2: STORAGE
  K3s + Local ArgoCD #1            K3s + Local ArgoCD #2
  syncs infrastructure/compute     syncs infrastructure/storage
    • LLM   (llama-server)           • Qdrant (vector DB)
    • Embed (llama-server)           • Redis  (session/chat)
```

Cross-cluster traffic is routed over an encrypted Tailscale mesh network. The
RAG application (LangChain / FastAPI) consumes the LLM + embedding endpoints
from the compute cluster and the Qdrant + Redis endpoints from the storage
cluster.

## Models & Resource Budget

| Service                        | Model / Image                | Quant     | RAM      |
| ------------------------------ | ---------------------------- | --------- | -------- |
| LLM `llama-server`             | Qwen2.5-1.5B-Instruct GGUF   | `Q4_K_M`  | ~1.10 GB |
| Embedding `llama-server`       | nomic-embed-text-v1.5 GGUF   | `Q4_K_M`  | ~0.28 GB |
| ArgoCD (standalone, per host)  | Full K8s stack               | —         | ~0.35 GB |
| Linux OS + K3s core            | Minimal Debian/Ubuntu        | —         | ~0.80 GB |

Runtime tuning for dual-core hosts: `-t 2` (2 threads) and `-c 2048`
(2048-token context window) to eliminate thread contention.

## Repository Layout

```text
home-lab-rag-monorepo/
├── README.md
├── infrastructure/
│   ├── compute/        # synced EXCLUSIVELY by Cluster 1's ArgoCD
│   └── storage/        # synced EXCLUSIVELY by Cluster 2's ArgoCD
└── apps/
    ├── rag-backend/    # LangChain / FastAPI service
    └── pipeline/       # CI/CD workflows
```

## Quick Start

See [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) for the full bootstrap workflow.

1. Install K3s + a standalone ArgoCD instance on each host.
2. Edit the `repoURL` in the two root Application manifests to point at your fork.
3. Apply `infrastructure/compute/root-app-compute.yaml` to Cluster 1.
4. Apply `infrastructure/storage/root-app-storage.yaml` to Cluster 2.
5. ArgoCD reconciles state, InitContainers pull the GGUF models, and the
   `llama-server` endpoints come up on NodePorts 30080 (LLM) / 30081 (embedding).

## Development & Testing

The RAG backend has a unit-test suite under `apps/rag-backend/tests/` (23 tests)
covering the chunker, deterministic point IDs, knowledge-base loading, ingestion
idempotency, the HTTP endpoints, and the `/chat` retrieval + out-of-scope guard
pipeline.

```bash
cd apps/rag-backend
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The tests are hermetic: they stub `qdrant_client` and use in-memory fakes for
Redis/Qdrant and mock the LLM/embedding HTTP calls, so no cluster, model, or
network access is required. This also side-steps the numpy SIMD requirement of
the real `qdrant-client` on legacy CPUs.

## Maintenance

Change parameters (thread counts, model versions, storage limits) under
`infrastructure/compute` or `infrastructure/storage`, then `git push`. The
corresponding ArgoCD instance reconciles its cluster independently.
