# genus-os Helm chart

<!-- Staging URL: https://genus-os.staging.internal.ironsail.ai -->

Deploys the in-cluster subset of Genus OS — agent engine, bridge, orchestrator,
the Helm dashboard, optional NATS, and toggleable in-cluster Postgres + Redis
(as Helm dependencies on `groundhog2k/postgres` and `groundhog2k/redis` — no
Bitnami, no operator). Out of scope: vision, voice, MediaMTX, desktop-use, and
the Ollama server. Engine/Orchestrator can target an operator-managed local
model endpoint or configured cloud provider.

For managed cloud data services, set `postgres.enabled: false` and
`redis.enabled: false`, put the complete `ROBOTHOR_DB_*` and
`ROBOTHOR_REDIS_*` connection material in their separate Vault paths, install
the trusted database CA, and declare the exact endpoint CIDRs/ports in the
environment values. Plain chart values are not the credential source in this
mode.

## Files

| File | Purpose |
|---|---|
| `Chart.yaml` | Chart metadata. `version` + `appVersion` bumped by semantic-release. |
| `values.yaml` | Layer 1 — chart defaults. |
| `values-staging.yaml` | Layer 2 — staging policy, secret paths, topology, and image/breadcrumb state. An explicitly labeled preview uses `pr-N-sha-<short>` and resets on close/unlabel. |
| `values-production.yaml` | Layer 2 — fail-closed production policy and the last image tag promoted after both release images pass their blocking scans. |
| `values-local.yaml` | Layer 2 — minikube / kind / Docker Desktop overrides. |

An infrastructure repository may supply a Layer-3 override for cluster-specific
hosts, existing claims, exact CIDRs, and resource placement. Layer 3 must narrow
or complete the Layer-2 policy; it must not collapse secret classes or disable
the production/staging authentication and NetworkPolicy gates.

## Release-candidate boundary

Rendering the production values is not evidence that the installation is
production-ready. The Engine intentionally runs one replica and uses a
`Recreate` strategy because it owns scheduler and consumer state. This creates
an availability interruption during restart/failure; PDBs and extra replicas
for other services do not establish automatic failover or a 99.9% SLO.

The 15-minute RPO and 60-minute RTO are targets until scheduled off-site
snapshots and a timed restore drill measure them. A chart install also does not
establish PCI, HIPAA, SOC 2, or any other certification.

Submit changes through a draft PR and let the required checks finish. Merge and
deployment require separate human approval. A draft PR without the explicit
staging label does not deploy this chart.

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
migrations.enabled: true      # Migration Job + wait-for-migrations init
vault.enabled: true           # VaultStaticSecret via Vault Secrets Operator
networkPolicy.enabled: false  # production/staging layer-2 values enable ingress + egress policy
```

Production and staging materialize separate Vault paths for each trust class.
Each workload opts in through `secretRefs`; unknown or forbidden references
fail Helm rendering. The dashboard is statically restricted to
`dashboard-auth` and `bridge-sso`, while migrations are statically restricted
to `database`:

```yaml
vault:
  secrets:
    database: {path: genus-os/production/database}
    cache: {path: genus-os/production/cache}
    auth-signing: {path: genus-os/production/auth-signing}
    bridge-sso: {path: genus-os/production/bridge-sso}
    bridge-oidc: {path: genus-os/production/bridge-oidc}
    dashboard-auth: {path: genus-os/production/dashboard-auth}
    engine-providers: {path: genus-os/production/engine-providers}
    bridge-integrations: {path: genus-os/production/bridge-integrations}
    orchestrator-providers: {path: genus-os/production/orchestrator-providers}

dashboard:
  secretRefs: [dashboard-auth, bridge-sso]
migrations:
  secretRefs: [database]
```

VSO mirrors a path's keys verbatim, so path contents are also an authorization
boundary. Keep them limited to these classes:

| Class | Intended keys / consumers |
|---|---|
| `database` | `ROBOTHOR_DB_HOST`, `ROBOTHOR_DB_PORT`, `ROBOTHOR_DB_NAME`, `ROBOTHOR_DB_USER`, `ROBOTHOR_DB_PASSWORD`; Python services and migrations |
| `cache` | `ROBOTHOR_REDIS_HOST`, `ROBOTHOR_REDIS_PORT`, `ROBOTHOR_REDIS_PASSWORD`; Python services only |
| `auth-signing` | `GENUS_AUTH_SIGNING_KEY`; Engine and Bridge enforce signed identity. It is currently also mounted to Orchestrator, which must remain network-restricted until it independently verifies tokens. |
| `bridge-sso` | `GENUS_BRIDGE_SSO_SECRET`; Bridge and dashboard BFF only |
| `bridge-oidc` | `GENUS_OIDC_ISSUERS`; Bridge only |
| `dashboard-auth` | `AUTH_SECRET`, OIDC issuer/client ID/client secret/name, optional `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD` (sign in via a fronting Cloudflare Access policy instead of a second IdP prompt); dashboard only |
| `engine-providers` | LLM, delivery, and tool-provider tokens required by Engine only |
| `bridge-integrations` | Bridge webhook/integration tokens only |
| `orchestrator-providers` | retrieval/reranking provider tokens required by Orchestrator only |

Never put raw PAN/CVV, customer payment data, JWT signing material, database
credentials, or provider tokens into a dashboard-readable path. Owner/entity
payment integrations may store only provider-issued virtual-card references or
authorization tokens in the owning backend's path. Ownership does not exempt a
card from PCI scope or make raw PAN/CVV safe for Genus OS to store. A missing
referenced Secret keeps the workload in `CreateContainerConfigError`; there is
no plaintext or chart-wide fallback.

The chart creates separate ServiceAccounts for Engine, Bridge, Orchestrator,
dashboard, migrations, NATS, and the Helm smoke pod. Tokens are disabled by
default. Python pods project a short-lived token only into their read-only,
release-Job-scoped migration watcher init container; the application container
cannot mount it. The migration Job and dashboard never receive Kubernetes
service-account API tokens.

## Authentication and generated UI

Private ingress is not the application authentication mechanism:

- The dashboard requires Auth.js OIDC configuration and a successful
  Bridge-authenticated session exchange. Existing users require an explicit
  issuer/subject binding; verified email does not silently link accounts.
- Bridge verifies signed audience/expiry/tenant/role/scope claims and enforces
  route-specific scopes and tenant restrictions.
- Engine independently verifies signed, same-tenant `engine:*` authority for
  every non-probe HTTP request and the IDE WebSocket. Webhook routes retain
  their provider-specific HMAC check.
- Orchestrator does not yet independently verify this signed identity contract.
  Keep it ClusterIP-only and NetworkPolicy-restricted; do not add direct
  ingress until that gap is closed.

The Next.js dashboard has no model-provider credentials, URL, model selection,
or provider headers. It forwards the verified Bridge bearer token to the
Engine's same-tenant `POST /api/dashboard/completions`; Engine owns provider
selection and returns content only. Generated dashboard HTML is read-only:
sanitizers and the sandbox reject scripts, external resources, links, forms,
buttons/inputs/selects, event handlers, network calls, and action channels.
Mutations remain limited to native authenticated application routes.

## Workspace and readiness

The engine mounts the entity workspace at `/workspace`. Local values use an
ephemeral workspace and explicitly permit an empty fleet. Staging and
production create a PVC, require at least one valid manifest, and require the
`main` control agent before `/ready` succeeds.

For an existing populated volume:

```yaml
workspace:
  allowEmptyFleet: false
  requiredAgentIds: [main]
  persistence:
    enabled: true
    existingClaim: genus-os-workspace
```

An immutable deployment can instead set `workspace.configMap.name`, map keys to
nested paths with `workspace.configMap.items`, and leave persistence disabled.
It must include `docs/agents/main.yaml` and its referenced instruction files. A new
chart-created PVC intentionally starts unready until an operator or provisioning
job seeds the workspace; an empty agent fleet is never reported as production
ready.

`/live` is process-only. `/ready` verifies required dependencies and fleet
configuration. Legacy `/health` and `/liveness` endpoints remain available for
older integrations.

## Database migration prerequisites

The migration Job uses the sole packaged 83-entry manifest. It takes an
advisory lock, records full IDs and checksums, and refuses drift or unknown
history. Upgrade safeguards preserve legacy memory tables as archives and
require a 30-day replacement-data gate plus a full-row archive before removing
legacy score columns.

The chain requires PostgreSQL extensions `vector`, `uuid-ossp`, `citext`, and
`pgcrypto`. Managed services must pre-provision them or grant the migration
identity sufficient extension permission. Before rollout, run the full chain
against a production clone, compare material row counts, take and verify an
encrypted snapshot, and rehearse restore. The migration chain is forward-only;
rolling back an image does not reverse schema or external side effects.

Production and staging set PostgreSQL `sslmode=verify-full`; the database
endpoint must present a certificate trusted by the container's CA bundle. Their
NetworkPolicies default-deny ingress and egress, then rebuild only the declared
component graph. In-chart PostgreSQL/Redis are selected by pod labels. Managed
database, Redis, Ollama, HTTPS API, or egress-proxy access requires exact CIDRs
and ports in Layer 3:

```yaml
networkPolicy:
  egress:
    destinations:
      kubernetes-api:
        # Exact kubernetes.default Service ClusterIP for migration watchers.
        cidrs: [10.43.0.1/32]
      database:
        cidrs: [10.42.16.8/32]
      redis:
        cidrs: [10.42.24.0/28]
      payments:
        # Exact hosted-tokenization / virtual-card issuer API or proxy range.
        cidrs: [10.42.26.20/32]
      identity:
        # Dashboard may leave the cluster only for the exact IdP endpoint.
        cidrs: [10.42.28.12/32]
      # Prefer the organization's controlled egress proxy range. Configure
      # HTTP(S)_PROXY only on backend component env blocks that need it.
      https:
        cidrs: [10.42.32.10/32]
        ports: [{protocol: TCP, port: 8443}]
```

Empty CIDR lists fail closed. `0.0.0.0/0` and `::/0` are rejected during Helm
rendering, and staging/production cannot disable ingress/egress NetworkPolicy.
Before rollout, verify the Layer-3 CIDRs include the managed DB and
cache endpoints plus only the approved API gateway/egress proxy. Dashboard is
forbidden from every external destination except the identity class, so it
cannot use the provider/API egress rule. DNS is limited to the configured
CoreDNS pod selector.

## Local dev

```bash
helm repo add groundhog2k https://groundhog2k.github.io/helm-charts/
helm dependency update helm/genus-os

helm install gos helm/genus-os \
  --namespace genus --create-namespace \
  --values helm/genus-os/values.yaml \
  --values helm/genus-os/values-local.yaml \
  --set vault.enabled=false

# values-local disables VSO. Provide one Secret per referenced trust class;
# never combine them into one broad local Secret. At minimum:
kubectl -n genus create secret generic gos-genus-os-database \
  --from-literal=ROBOTHOR_DB_PASSWORD=devpass
kubectl -n genus create secret generic gos-genus-os-cache \
  --from-literal=ROBOTHOR_REDIS_PASSWORD=""
kubectl -n genus create secret generic gos-genus-os-auth-signing \
  --from-literal=GENUS_AUTH_SIGNING_KEY='local-only-32-byte-minimum-key-value'
# Create the remaining component-specific Secrets named in `secretRefs` with
# only their documented keys, or override secretRefs for the local features
# you actually run.

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

Semantic release first updates product metadata, including `Chart.yaml`, but
deliberately leaves production/staging deployment tags on the last promoted
image. The release workflow builds the semantic-release tag for both Python and
dashboard images and blocks on their scans. Only after both image tags exist
does `scripts/promote-release-values.js` update production (and idle staging)
to `vX.Y.Z` in a separate GitOps promotion commit. A failed production smoke
test reverts that promotion commit and syncs ArgoCD; emergency Argo rollback is
only the fallback. Schema and external side effects still require a compatible
forward-fix/restore plan.
