# Giving ArgoCD Access to a Private GitHub Repository

When `home-lab-rag-monorepo` is private, each standalone ArgoCD instance needs
read credentials to clone it. This guide covers the three supported methods and
how to keep the credentials safe.

> Because this project runs **two independent ArgoCD instances** (one on the
> compute cluster, one on the storage cluster), you must register the
> credential in the `argocd` namespace on **both** clusters. Read-only access
> is always sufficient — never grant write.

Example templates live next to this doc:

- `infrastructure/bootstrap/repo-secret-https.example.yaml` (PAT)
- `infrastructure/bootstrap/repo-secret-ssh.example.yaml` (deploy key)

Copy a template to `repo-secret.yaml` (git-ignored), fill it in, and apply it.

---

## How ArgoCD recognises a repo credential

A `Secret` in the `argocd` namespace carrying the label
`argocd.argoproj.io/secret-type: repository` is picked up automatically as a
repository credential. The `url` in the Secret must match the `repoURL` in your
Application manifests.

---

## Option 1 — HTTPS + Personal Access Token (simplest)

**Create the token**
- Fine-grained PAT: scope it to the `home-lab-rag-monorepo` repository only,
  with permission **Contents: Read-only**.
- Or a classic token with the `repo` scope.

**Register it — CLI**
```bash
argocd repo add https://github.com/your-user/home-lab-rag-monorepo.git \
  --username your-user \
  --password ghp_yourTokenHere
```

**Register it — declarative** (`repo-secret-https.example.yaml`)
```bash
cp infrastructure/bootstrap/repo-secret-https.example.yaml repo-secret.yaml
# edit username + password (the PAT)
kubectl apply -n argocd -f repo-secret.yaml   # run on BOTH clusters
```

Your Application `repoURL` stays in `https://github.com/...` form.

---

## Option 2 — SSH deploy key (least privilege for a single repo)

**Generate a key pair**
```bash
ssh-keygen -t ed25519 -f argocd_ticino -N "" -C "argocd@homelab"
```

**Add the public key to GitHub**
Repo → Settings → Deploy keys → Add deploy key → paste `argocd_ticino.pub`,
leave **Allow write access unchecked**.

**Register the private key — CLI**
```bash
argocd repo add git@github.com:your-user/home-lab-rag-monorepo.git \
  --ssh-private-key-path ./argocd_ticino
```

**Register it — declarative** (`repo-secret-ssh.example.yaml`)
```bash
cp infrastructure/bootstrap/repo-secret-ssh.example.yaml repo-secret.yaml
# paste the PRIVATE key (contents of argocd_ticino) into sshPrivateKey
kubectl apply -n argocd -f repo-secret.yaml   # run on BOTH clusters
```

> With SSH you MUST switch the Application `repoURL` to the SSH form
> `git@github.com:your-user/home-lab-rag-monorepo.git` in both
> `root-app-compute.yaml` and `root-app-storage.yaml`.

A deploy key is scoped to one repo, which makes it a clean least-privilege fit
for a home lab.

---

## Option 3 — GitHub App (most robust, best at scale)

Register a GitHub App with **Contents: Read-only**, install it on the repo, and
give ArgoCD the App ID, installation ID, and private key. Tokens are
short-lived and auto-rotated.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: metaxploit-rag-ghapp
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: https://github.com/your-user/home-lab-rag-monorepo.git
  githubAppID: "123456"
  githubAppInstallationID: "7891011"
  githubAppPrivateKey: |
    -----BEGIN RSA PRIVATE KEY-----
    ...
    -----END RSA PRIVATE KEY-----
```

This is the cleanest for many repos or an organisation, but usually overkill for
a two-cluster home lab.

---

## Keeping credentials out of Git

Committing a raw token or private key into the repo is a real risk. Pick one:

- **Apply out-of-band (simplest for a home lab):** keep `repo-secret.yaml`
  git-ignored and `kubectl apply` it during bootstrap. Not tracked in Git.
- **Sealed Secrets (Bitnami):** encrypt the Secret with the cluster's public
  key so only that cluster can decrypt it — the encrypted `SealedSecret` is
  safe to commit and reconcile via GitOps.
- **External Secrets Operator:** store the credential in a vault (e.g. Vault,
  AWS Secrets Manager) and sync it into the cluster at runtime.

The provided `.gitignore` already blocks `repo-secret.yaml`, `*-secret.yaml`
(except `*-secret.example.yaml`), `*.pem`, `*.key`, and generated `argocd_*`
key files, so filled-in credentials won't be committed by accident.

---

## Verify access

After applying the credential on a cluster:

```bash
# List registered repos and their connection status
argocd repo list

# Or check the root application syncs cleanly
kubectl -n argocd get applications
argocd app get cluster-compute-orchestrator   # on the compute cluster
argocd app get cluster-storage-orchestrator   # on the storage cluster
```

A healthy repo shows `Connection Status: Successful`; the Application should
move out of `Unknown`/`ComparisonError` into `Synced`.
