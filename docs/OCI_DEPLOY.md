# Deploy su cluster K3s (OCI) via ArgoCD

Guida passo-passo per far girare `home-lab-rag-monorepo` sul cluster K3s
single-node-per-ruolo su OCI (master + 2 worker), usando ArgoCD già installato
lì (vedi `oci/docs/ARGOCD_INSTALL.md` nel repo `oci/`).

Rispetto al design originale a 2 cluster + Tailscale, qui è **un solo
cluster**: stessa logica già validata in locale con minikube
(`docs/LOCAL_MINIKUBE_DEPLOY.md`) — i tre namespace (`compute`, `storage`,
`rag`) comunicano via DNS interno, non serve nessuna VPN.

## 0. Cosa NON serve cambiare (verificato)

- `repoURL` in `infrastructure/compute/root-app-compute.yaml` e
  `infrastructure/storage/root-app-storage.yaml` — già corretto:
  `https://github.com/alejanmat/devops-assistant.git`, combacia col
  `git remote` del progetto.
- `destination.server: https://kubernetes.default.svc` in entrambi — corretto
  così com'è, perché ArgoCD gira nello **stesso** cluster che gestisce
  (in-cluster reference, non serve l'IP del master).
- `syncPolicy`, `syncOptions: CreateNamespace=true`, `exclude` — invariati.
- Storage: K3s usa `local-path-provisioner` con `VolumeBindingMode:
  WaitForFirstConsumer` (verificato su questo cluster) — a differenza del
  bug hostpath incontrato su minikube multi-nodo, qui il volume viene creato
  solo *dopo* che il pod è già stato schedulato su un nodo, quindi nessun
  mismatch cross-node. Nessun `nodeSelector` necessario.
- Gli endpoint di default già in `apps/rag-backend/main.py`
  (`http://llama-llm-nodeport.compute:8080`, ecc.) — validi anche qui, stesso
  motivo del test minikube.

## 1. Commit + push del repo (prerequisito bloccante)

```bash
cd /Users/matias.plumari/Development/devops/homelab-rag-agent
git status   # al momento: "No commits yet"
git add .
git commit -m "initial commit"
git push -u origin main
```

ArgoCD sincronizza da Git: senza questo step non ha nulla da leggere.

## 2. Build + push dell'immagine `rag-backend`

Su minikube avevamo usato `minikube image build` (build diretta nel Docker
daemon del cluster) — su un cluster remoto reale questa scorciatoia non
esiste, serve un **registry pubblico** (GHCR, già referenziato dalla
pipeline in `apps/pipeline/ci-cd-workflow.yaml` e dal placeholder
`ghcr.io/your-user/home-lab-rag-backend:latest` nel deployment).

```bash
cd apps/rag-backend

# login a GHCR (serve un Personal Access Token con scope write:packages)
echo "<il-tuo-github-token>" | docker login ghcr.io -u <tuo-username-github> --password-stdin

docker build -t ghcr.io/<tuo-username-github>/devops-rag-backend:latest .
docker push ghcr.io/<tuo-username-github>/devops-rag-backend:latest
```

In alternativa, lascia fare alla pipeline CI/CD già presente
(`.github/workflows` o `apps/pipeline/ci-cd-workflow.yaml`) al primo push su
`main` — controlla quel file per capire se è già configurata a build/push
automatico.

## 3. Usa il manifest già adattato per OCI

Il file originale `apps/rag-backend/k8s/deployment-app.yaml` resta il
template di riferimento per il design multi-cluster con Tailscale (non
va modificato). Per questo deploy single-cluster su OCI usa invece:

- `apps/rag-backend/k8s/deployment-app.oci.yaml` — già con:
  - env var puntate ai nomi DNS in-cluster (`llama-llm-nodeport.compute`,
    `qdrant-nodeport.storage`, ecc.) invece dei Tailscale hostname
  - `imagePullPolicy: Always` (serve un pull reale da registry)
  - `Service` di tipo `ClusterIP` invece di `NodePort` (l'accesso pubblico
    passa dall'Ingress, deciso al punto 4)
- `apps/rag-backend/k8s/ingress.yaml` — Ingress Traefik per `rag-backend`

**Unica cosa da modificare a mano** in `deployment-app.oci.yaml`:
```yaml
image: ghcr.io/<tuo-username-github>/devops-rag-backend:latest
```
sostituisci `<tuo-username-github>` con l'immagine che hai buildato/pushato
al punto 2.

## 4. Accesso esterno: Ingress via Traefik (deciso)

La Security List OCI apre il range NodePort (30000-32767) **solo da dentro
la VCN** — un `Service` NodePort non sarebbe raggiungibile dal tuo Mac come
lo era su minikube. Scelto invece l'Ingress via Traefik (porte 80/443 già
pubbliche, già bundlato in K3s): un solo punto di ingresso, routing su
path, coerente con l'architettura Traefik già prevista dal progetto — vedi
`apps/rag-backend/k8s/ingress.yaml` creato al punto 3.

## 5. Applica i root Application ad ArgoCD

```bash
export KUBECONFIG=/Users/matias.plumari/Development/devops/oci/kubeconfig-k8s-oci.yaml

kubectl apply -f infrastructure/compute/root-app-compute.yaml
kubectl apply -f infrastructure/storage/root-app-storage.yaml
```

ArgoCD ora sincronizza automaticamente `infrastructure/compute/` e
`infrastructure/storage/` dal repo Git (grazie a `syncPolicy.automated`).

## 6. Applica il deployment di `rag-backend`

Non c'è (ancora) un root Application dedicato a `apps/rag-backend/` nel
progetto — per ora applicalo direttamente:

```bash
kubectl apply -f apps/rag-backend/k8s/deployment-app.oci.yaml
kubectl apply -f apps/rag-backend/k8s/ingress.yaml
```

(Se vuoi renderlo GitOps al 100%, si può creare un terzo root Application
`root-app-rag.yaml` che punta a `path: apps/rag-backend/k8s` — dimmi se lo
vuoi e te lo preparo.)

## 7. Verifica

```bash
# nella UI ArgoCD (port-forward o Ingress, vedi oci/docs/ARGOCD_INSTALL.md):
# le due Application "cluster-compute-orchestrator" e
# "cluster-storage-orchestrator" devono apparire Synced + Healthy

kubectl get pods -n compute -o wide
kubectl get pods -n storage -o wide
kubectl get pods -n rag -o wide

# smoke test via Ingress (IP pubblico del master, porta 80, nessun NodePort)
curl -s http://129.152.6.131/health
curl -s -X POST http://129.152.6.131/chat \
  -H "content-type: application/json" \
  -d '{"message":"How should I fish for perch in the Ticino near Pavia?"}' | jq .
```
