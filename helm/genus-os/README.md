# genus-os Helm chart

Deploys the in-cluster subset of Genus OS — agent engine, bridge, orchestrator, the Helm dashboard, optional NATS, and toggleable in-cluster Postgres + Redis (as Helm dependencies on `groundhog2k/postgres` and `groundhog2k/redis` — no Bitnami, no operator). Out of scope: vision, voice, MediaMTX, desktop-use, Ollama (use OpenRouter / Anthropic / Gemini APIs instead).

For cloud envs, set `postgres.enabled: false` and `redis.enabled: false` in the layer-3 values file (infra repo) and point `database.host` / `redis.host` at the managed equivalents (RDS / ElastiCache / CloudNativePG).

## Files

| File | Purpose |
|---|---|
| `Chart.yaml` | Chart metadata. `version` + `appVersion` bumped by semantic-release. |
| `values.yaml` | Layer 1 — chart defaults. |
| `values-staging.yaml` | Layer 2 — `global.imageTag` only. Bumped by the staging-deploy workflow (`pr-N-sha-<short>`) or reset to latest `vX.Y.Z` on PR close/unlabel. |
| `values-production.yaml` | Layer 2 — `global.imageTag` bumped by semantic-release on `main`. |
| `values-local.yaml` | Layer 2 — minikube / kind / Docker Desktop overrides. |

Per-env config (ingress hosts, vault paths, replica counts, resource limits) is Layer 3, lives in `aws-infrastucture/argocd/ironsail-<env>-genus-os/values.yaml`.

## Toggles

Every component is independently disable-able:

```yaml
engine.enabled: true
bridge.enabled: true
orchestrator.enabled: true
dashboard.enabled: true
nats.enabled: false           # federation — opt-in
postgres.enabled: true        # groundhog2k/postgres subchart — disable to use RDS
redis.enabled: true           # groundhog2k/redis subchart — disable to use ElastiCache
migrations.enabled: true      # Helm pre-install/pre-upgrade hook
externalSecrets.enabled: true # ExternalSecret refs to Vault
networkPolicy.enabled: false  # opt-in per env
```

## Local dev

```bash
helm repo add groundhog2k https://groundhog2k.github.io/helm-charts/
helm dependency update helm/genus-os

helm install gos helm/genus-os \
  --namespace genus --create-namespace \
  --values helm/genus-os/values.yaml \
  --values helm/genus-os/values-local.yaml \
  --set externalSecrets.enabled=false

# Provide the credentials Secret out-of-band (values-local disables ESO):
kubectl -n genus create secret generic gos-genus-os-credentials \
  --from-literal=ROBOTHOR_DB_PASSWORD=devpass \
  --from-literal=ANTHROPIC_API_KEY=sk-... \
  --from-literal=ROBOTHOR_REDIS_PASSWORD=""

helm test gos --namespace genus
```

## Linting & tests

CI runs `helm lint`, `helm unittest`, `kubeconform` (schema validation), and `kube-linter` (best practices) on every PR that touches `helm/`. Run locally:

```bash
helm lint helm/genus-os --strict --values helm/genus-os/values.yaml
helm unittest helm/genus-os --color --strict
helm template gos helm/genus-os --values helm/genus-os/values.yaml \
  | kubeconform -strict -summary -kubernetes-version 1.31.0
```

Install plugins once: `helm plugin install https://github.com/helm-unittest/helm-unittest`.

## Versioning

`Chart.yaml`'s `version` and `appVersion` are bumped by `scripts/update-helm-values.sh` (called from `.releaserc.js`'s `@semantic-release/exec` step). The same script bumps `global.imageTag` in `values-production.yaml`. Both happen in the same release commit.
