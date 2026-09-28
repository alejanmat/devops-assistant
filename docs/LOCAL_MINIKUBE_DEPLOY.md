# Deploy locale su minikube (single-cluster)

Guida passo-passo per far girare l'intero sistema RAG "Ticino Angler" su un
singolo cluster minikube in locale, senza Tailscale/ArgoCD/hardware reale.
Utile per testare rapidamente che l'app funzioni prima di un deploy sui due
cluster K3s reali descritto in [`BOOTSTRAP.md`](./BOOTSTRAP.md).

## Cosa verrà creato

Un solo cluster minikube con 3 namespace, che riproducono la separazione dei
due cluster reali del progetto:

| Namespace | Contiene | Corrisponde a |
|---|---|---|
| `compute` | `llama-llm` (Qwen2.5-1.5B), `llama-embedding` (nomic-embed-text) | Cluster 1 (Compute) reale |
| `storage` | `qdrant` (vector DB), `redis` (sessioni chat) | Cluster 2 (Storage) reale |
| `rag` | `rag-backend` (FastAPI + LangChain) | L'app RAG stessa |

In un setup single-cluster i tre namespace comunicano via DNS interno
(`<service>.<namespace>`) invece che via Tailscale mesh — i default già
presenti in `apps/rag-backend/main.py` puntano esattamente a questi nomi,
quindi non serve modificare nulla nel codice.

## Prerequisiti

- Docker Desktop attivo
- `minikube`, `kubectl` installati
- Connessione internet (i modelli GGUF vengono scaricati da HuggingFace al
  primo avvio: ~1.1GB per l'LLM, ~0.3GB per l'embedding)

## 1. Creare il cluster minikube

```bash
minikube start -p rag-lab --driver=docker --memory=4096 --cpus=4
kubectl get nodes -o wide   # verifica che sia "Ready"
```

## 2. Creare i namespace

```bash
kubectl create namespace compute
kubectl create namespace storage
kubectl create namespace rag
```

## 3. Deployare lo storage (Qdrant + Redis)

```bash
cd homelab-rag-agent   # root del progetto

kubectl apply -f infrastructure/storage/pvcs-storage.yaml
kubectl apply -f infrastructure/storage/redis-deployment.yaml
kubectl apply -f infrastructure/storage/qdrant-statefulset.yaml
kubectl apply -f infrastructure/storage/services-nodeport.yaml
```

Attendi che siano pronti:

```bash
kubectl get pods -n storage -w
# CTRL+C quando qdrant-0 e redis-... sono entrambi 1/1 Running
```

## 4. Deployare il compute (LLM + Embedding)

```bash
kubectl apply -f infrastructure/compute/pvc-models.yaml
kubectl apply -f infrastructure/compute/deployment-llm.yaml
kubectl apply -f infrastructure/compute/deployment-embedding.yaml
kubectl apply -f infrastructure/compute/services-nodeport.yaml
```

Il primo avvio scarica realmente i due modelli GGUF da HuggingFace
(initContainer `download-model`) — può richiedere qualche minuto in base
alla connessione. Segui il progresso con:

```bash
kubectl logs -n compute -l app=llama-llm -c download-model -f
```

Poi attendi che i pod applicativi siano pronti (readiness probe su
`/health`, con parecchio margine per il caricamento del modello):

```bash
kubectl get pods -n compute -w
# CTRL+C quando llama-llm-deployment-... e llama-embedding-deployment-... sono 1/1 Running
```

## 5. Buildare l'immagine del backend RAG dentro minikube

Niente bisogno di un registry esterno: si builda l'immagine direttamente
nel Docker daemon del cluster.

```bash
cd apps/rag-backend
minikube image build -p rag-lab -t rag-backend:local .
cd ../..
```

## 6. Deployare il backend RAG

Il manifest originale (`apps/rag-backend/k8s/deployment-app.yaml`) è pensato
per il deploy reale via Tailscale — per il test locale va usata una copia con
l'immagine e gli endpoint adattati al single-cluster:

```bash
cp apps/rag-backend/k8s/deployment-app.yaml /tmp/deployment-app.local.yaml
```

Poi modifica in `/tmp/deployment-app.local.yaml`:

- `image: ghcr.io/your-user/home-lab-rag-backend:latest` → `image: rag-backend:local`
- aggiungi subito sotto `image:` la riga `imagePullPolicy: Never`
- gli host `compute-node`/`storage-node` nelle env var → nomi DNS in-cluster:
  - `LLM_BASE_URL` → `http://llama-llm-nodeport.compute:8080`
  - `EMBEDDING_BASE_URL` → `http://llama-embedding-nodeport.compute:8080`
  - `QDRANT_URL` → `http://qdrant-nodeport.storage:6333`
  - `REDIS_URL` → `redis://redis-nodeport.storage:6379/0`

Applica:

```bash
kubectl apply -f /tmp/deployment-app.local.yaml
```

Il pod, all'avvio, ingerisce automaticamente la knowledge base
(`KB_AUTO_INGEST=true` di default) chiamando l'embedding service e scrivendo
i vettori su Qdrant. Segui i log per confermare:

```bash
kubectl logs -n rag -l app=rag-backend -f
# cerca la riga: "Ingested N knowledge-base chunks..." e "...RAG backend ready."
```

Attendi che sia pronto:

```bash
kubectl get pods -n rag -w
# CTRL+C quando rag-backend-... è 1/1 Running
```

## 7. Smoke test

```bash
kubectl port-forward -n rag svc/rag-backend 8000:8000 &

curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready
curl -s http://localhost:8000/sources | jq .

curl -s -X POST http://localhost:8000/chat \
  -H "content-type: application/json" \
  -d '{"message":"How should I fish for perch in the Ticino near Pavia?"}' | jq .

kill %1   # chiude il port-forward
```

Se `/chat` risponde con un `answer` pertinente e un array `sources` con
`score` intorno a 0.7-0.9, il sistema funziona correttamente end-to-end.

---

# Comandi per esplorare il deploy

## Panoramica generale

```bash
kubectl get ns | grep -E "compute|storage|rag"

for n in compute storage rag; do echo "=== $n ==="; kubectl get all -n $n; done
```

## Storage (PVC/PV)

```bash
kubectl get pvc -A
kubectl get pv
kubectl get storageclass
```

## Pod: stato, log, dettagli

```bash
kubectl get pods -n compute -o wide
kubectl get pods -n storage -o wide
kubectl get pods -n rag -o wide

# log (usa -f per seguirli in tempo reale)
kubectl logs -n rag -l app=rag-backend -f
kubectl logs -n compute -l app=llama-llm
kubectl logs -n compute -l app=llama-embedding
kubectl logs -n storage -l app=qdrant
kubectl logs -n storage -l app=redis

# log dell'initContainer che scarica i modelli GGUF
kubectl logs -n compute -l app=llama-llm -c download-model

# describe (utile per troubleshooting: sezione Events in fondo)
kubectl describe pod -n rag -l app=rag-backend
```

## Entrare dentro un container

```bash
kubectl exec -it -n rag deploy/rag-backend -- sh
kubectl exec -it -n storage deploy/redis -- redis-cli
kubectl exec -it -n storage statefulset/qdrant -- sh
```

## Servizi ed endpoint

```bash
kubectl get svc -A | grep -E "compute|storage|rag"
kubectl get endpoints -n compute
kubectl get endpoints -n storage
kubectl get endpoints -n rag
```

## Interagire con il RAG (`rag-backend`)

```bash
kubectl port-forward -n rag svc/rag-backend 8000:8000 &

# health/readiness
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready

# quali file della knowledge base sono caricati
curl -s http://localhost:8000/sources | jq .

# forza un re-ingest (utile se hai cambiato i file in knowledge_base/)
curl -s -X POST http://localhost:8000/reindex

# fai una domanda al RAG
curl -s -X POST http://localhost:8000/chat \
  -H "content-type: application/json" \
  -d '{"message":"Best bait for catfish in the Ticino?"}' | jq .

# continuare la stessa conversazione (usa il session_id ritornato sopra)
curl -s -X POST http://localhost:8000/chat \
  -H "content-type: application/json" \
  -d '{"message":"And what about at night?", "session_id":"<session_id-ricevuto-sopra>"}' | jq .

kill %1
```

## Interagire direttamente con LLM / Embedding / Qdrant / Redis (bypassando il backend)

```bash
# llama-server LLM (API compatibile OpenAI)
kubectl port-forward -n compute svc/llama-llm-nodeport 30080:8080 &
curl -s http://localhost:30080/health
curl -s -X POST http://localhost:30080/completion -H "content-type: application/json" -d '{"prompt":"Hello","n_predict":20}'

# llama-server Embedding
kubectl port-forward -n compute svc/llama-embedding-nodeport 30081:8080 &
curl -s -X POST http://localhost:30081/v1/embeddings -H "content-type: application/json" -d '{"input":"perch fishing"}'

# Qdrant (API + dashboard web)
kubectl port-forward -n storage svc/qdrant-nodeport 6333:6333 &
curl -s http://localhost:6333/collections | jq .
curl -s http://localhost:6333/collections/ticino_fishing | jq .
# apri http://localhost:6333/dashboard nel browser

# Redis
kubectl port-forward -n storage svc/redis-nodeport 6379:6379 &
redis-cli -p 6379 ping
redis-cli -p 6379 keys '*'
```

## Monitoraggio risorse (rilevante: il progetto è pensato per hardware "low-resource")

```bash
kubectl top pod -n compute
kubectl top pod -n storage
kubectl top pod -n rag
kubectl top node
```

## Pulizia

```bash
# rimuove solo le risorse applicate, tenendo il cluster
kubectl delete ns compute storage rag

# oppure elimina l'intero cluster di test
minikube delete -p rag-lab
```
