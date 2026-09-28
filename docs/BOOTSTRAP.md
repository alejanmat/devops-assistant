# Bootstrap Guide

Standing up the Low-Resource Multi-Cluster GitOps Home Lab RAG system.

## Prerequisites

- Two hosts (or two K3s clusters):
  - **Cluster 1 (Compute):** the legacy dual-core i5 with DDR3 RAM.
  - **Cluster 2 (Storage):** any host that can run Qdrant + Redis.
- A [Tailscale](https://tailscale.com/) account for the encrypted mesh between clusters.
- A GitHub (or GitLab) account with a fork of this monorepo.

## 1. Install K3s on each host

```bash
curl -sfL https://get.k3s.io | sh -
# kubeconfig lands at /etc/rancher/k3s/k3s.yaml
```

## 2. Join both hosts to the Tailscale mesh

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4   # note each node's mesh IP / MagicDNS name
```

Cross-cluster traffic (RAG backend -> LLM/embeddings/Qdrant/Redis) travels over
these Tailscale addresses via the NodePorts declared in the manifests.

## 3. Install a standalone ArgoCD instance on each cluster

Run this on **both** Cluster 1 and Cluster 2 (each ArgoCD is fully independent):

```bash
kubectl create namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

Retrieve the initial admin password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

## 4. Point the root Applications at your fork

Edit the `repoURL` in both files to your fork:

- `infrastructure/compute/root-app-compute.yaml`
- `infrastructure/storage/root-app-storage.yaml`

Commit and push.

## 5. Apply the root Applications (App-of-Apps)

On **Cluster 1 (Compute)**:

```bash
kubectl apply -f infrastructure/compute/root-app-compute.yaml
```

On **Cluster 2 (Storage)**:

```bash
kubectl apply -f infrastructure/storage/root-app-storage.yaml
```

Each ArgoCD instance now reconciles only its own subdirectory:

- Compute ArgoCD -> `infrastructure/compute` (LLM + embedding `llama-server`).
- Storage ArgoCD -> `infrastructure/storage` (Qdrant + Redis).

The compute InitContainers download the GGUF models on first boot, then the
`llama-server` endpoints come up on:

- `30080` — LLM (Qwen2.5-1.5B-Instruct)
- `30081` — Embeddings (nomic-embed-text-v1.5)

Storage NodePorts:

- `32333` — Qdrant HTTP
- `32379` — Redis

## 6. Deploy the RAG application

Build/push the image (handled by `apps/pipeline/ci-cd-workflow.yaml` on push),
then update the `image` and the `*_BASE_URL` / `*_URL` env values in
`apps/rag-backend/k8s/deployment-app.yaml` to your Tailscale hostnames and apply:

```bash
kubectl apply -f apps/rag-backend/k8s/deployment-app.yaml
```

## 7. Smoke test

```bash
# The Ticino fishing knowledge base auto-ingests on startup. If the embedding
# service was not ready at boot, load it on demand:
curl -s -X POST http://<rag-node>:30800/reindex

# See which knowledge-base files are loaded
curl -s http://<rag-node>:30800/sources

# Ask a domain question
curl -s -X POST http://<rag-node>:30800/chat \
  -H 'content-type: application/json' \
  -d '{"message":"How should I fish for perch in the Ticino near Pavia?"}'
```

## Maintenance

Change parameters (thread counts, model versions, storage limits) under
`infrastructure/compute` or `infrastructure/storage`, then `git push`. The
corresponding ArgoCD instance reconciles its cluster independently — no
cross-cluster impact.
