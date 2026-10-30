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

## Install

The chart is published to GHCR on every release, so nothing needs a clone:

```bash
helm install genus oci://ghcr.io/ironsail-llc/charts/genus-os \
  --version X.Y.Z \
  --namespace genus --create-namespace \
  --values values.yaml
```

The chart version is the release tag without its leading `v`; the publish job
refuses to push a chart whose version is not the tag, and pulls the pushed
artifact back and templates it with `values-production.yaml` before the job
goes green. `helm show values oci://ghcr.io/ironsail-llc/charts/genus-os
--version X.Y.Z` prints the defaults.

Installing from this directory (`helm install gos helm/genus-os …`) is the
developer path: it is what CI lints, and what to use for a change that has not
been released yet.

**Not in the chart yet:** plugins. `genus plugin sync` has no init container
here, so a cluster install activates plugins through the admin API rather than
through a values list. That belongs with the plugin installer, not with this
chart.

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
| `cache` | `ROBOTHOR_REDIS_HOST`, `ROBOTHOR_REDIS_PORT`, `ROBOTHOR_REDIS_PASSWORD`; Python services through `secretRefs`. The dashboard reads the cache password key only, for the event-bus stream reads: the chart wires that one key as a `secretKeyRef` (with `ROBOTHOR_REDIS_HOST`/`PORT` when the in-chart cache is enabled), and `dashboard.secretRefs` still refuses `cache` |
| `auth-signing` | `GENUS_AUTH_SIGNING_KEY`; Engine and Bridge enforce signed identity. It is currently also mounted to Orchestrator, which must remain network-restricted until it independently verifies tokens. |
| `bridge-sso` | `GENUS_BRIDGE_SSO_SECRET`; Bridge and dashboard BFF only |
| `bridge-oidc` | `GENUS_OIDC_ISSUERS`; Bridge only |
| `dashboard-auth` | `AUTH_SECRET`, OIDC issuer/client ID/client secret/name, optional `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD` (sign in via a fronting Cloudflare Access policy instead of a second IdP prompt); dashboard only. `AUTH_URL` is **not** a secret — it is the public origin and belongs in `dashboard.env`, set to `https://<dashboard.ingress.host>`; unset, sign-in redirects fall back to the container's `HOSTNAME=0.0.0.0` bind address. |
| `engine-providers` | LLM, delivery, and tool-provider tokens required by Engine only — including `BRAVE_SEARCH_API_KEY`, which `web_search` prefers over scraped engines (from a datacenter IP most of those are blocked, so in-cluster this key is the reliable path) |
| `bridge-integrations` | Bridge webhook/integration tokens only |
| `orchestrator-providers` | retrieval/reranking provider tokens required by Orchestrator only |
| `engine-google` | `credentials.json` only: an authorized-user OAuth file (`type`, `client_id`, `client_secret`, `refresh_token`) for the `gws_*` tools. Engine only, and only as a file mount through `engine.googleWorkspace`: `engine.secretRefs` refuses it, so the refresh token never becomes an environment variable. Declare it only where `engine.googleWorkspace.enabled` is on. |

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
- Local email+password sign-in (`global.localLogin`) is a supported
  alternative to OIDC for deployments with no identity provider. Owner MFA is
  mandatory for as long as it is on — `global.ownerMfaRequired: false` is the
  explicit opt-out, and nothing else turns the policy off, because the Bridge's
  issuer allowlist and the dashboard-only `CF_ACCESS_*` secrets cannot tell it
  whether a human has another way in. For the Bridge's per-IP sign-in limiter to
  see real client addresses, set `global.trustedProxies` to the dashboard pod's
  address (a `/32`, or a range a NetworkPolicy already fences off — a whole pod
  CIDR would grant every workload in the cluster the right to assert someone
  else's address) and `global.dashboardTrustedProxies` to the edge in front of
  the dashboard. Either left empty means no address is forwarded at all, which
  is the safe default; loopback is never trusted implicitly, so a same-host
  tunnel cannot let clients choose their own limiter key.
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

## Tenant cells

One release can run an engine per tenant. The root engine is the `engine:`
block and operates as `global.rootTenant` (default `default`, which is also
what the code assumes when nothing is set). Each entry in `tenants:` adds a
**cell**: one more engine for one child tenant, sharing the release's bridge,
dashboard, orchestrator, database and cache. The default `tenants: []` renders
nothing extra.

The list names real tenants, so it is deployment data: put it in your
environment's values, never in this chart.

```yaml
tenants:
  - id: child-a                  # a crm_tenants id: lowercase DNS label
  - id: child-b
    replicaCount: 1
    env: {SOME_FLAG: "1"}        # merged over engine.env
    workspace:
      requiredAgentIds: [main]
      persistence: {size: 5Gi}
    googleWorkspace: {enabled: true}
```

**What one entry renders.** A Deployment and a Service named
`<fullname>-engine-<id>`, and a workspace PVC `<fullname>-workspace-<id>`
unless the cell mounts a ConfigMap or an existing claim. A cell never renders
an Ingress. Its pods carry `app.kubernetes.io/component: engine`, like the
root engine's, so every NetworkPolicy written for the engine (ingress from the
bridge and dashboard, egress to the database, cache, NATS and external
destinations) covers them with no change. They are told apart by
`genus-os/tenant: <id>`, which is in each cell's selectors.

**The root engine.** Its Deployment selector is immutable and does not change.
Its pods gain the `genus-os/tenant: <rootTenant>` label, and once `tenants` is
non-empty its Service and PodDisruptionBudget select on it, so traffic for the
root engine never reaches a cell. If you add the first cell in the same
upgrade that first adds the label, the root Service has no endpoints until
the new root pod is ready. To avoid that gap, upgrade the chart first and add
cells afterwards.

**Tenant identity.** Every engine pod gets `ROBOTHOR_TENANT_ID` and
`ROBOTHOR_DEFAULT_TENANT` set to the same value: the cell's `id`, or
`global.rootTenant` on the root engine. The two must agree in one process
(`tenant_env_conflict` in `robothor/constants.py`), so a cell's `env` cannot
override them, and `engine.env` naming either one with a value other than
`global.rootTenant` fails at template time. A cell also gets
`GENUS_EVENT_CONSUMER_GROUP=engine-<id>`, so each engine reads the event
stream as its own consumer group.

**Reaching a cell.** The bridge and the dashboard both get
`ROBOTHOR_ENGINE_URL` (the root engine) and
`GENUS_TENANT_ENGINE_URL_TEMPLATE=http://<fullname>-engine-{tenant}:<engine.service.port>`.
`{tenant}` is literal text that the application replaces with a tenant id.
The chart refuses an id that would make `<fullname>-engine-<id>` longer than 63
characters, because the template could then name a Service that does not
exist. Both also get `ROBOTHOR_DEFAULT_TENANT=<global.rootTenant>`, so the
bridge, the dashboard and the root engine agree on which tenant is root and a
root user's requests go to the root engine rather than to a cell that does not
exist. If you set `ROBOTHOR_PLATFORM_TENANT`, set it on the bridge and the
dashboard alike: both read it before `ROBOTHOR_DEFAULT_TENANT`.

**Tenant switching.** Whether a root-tenant owner or admin may act in a
descendant tenant is one setting, read by the bridge (which issues the acting
token) and by every engine (which enforces it). Set it once and the chart
renders it on the bridge, the root engine and every cell. A component's own
`env`, or a cell's `env`, still wins.

| Value | Renders | Default |
|-------|---------|---------|
| `tenancy.switchMode` | `GENUS_TENANT_SWITCH_MODE`: `off`, `observe` or `enforce` | `""`, which renders nothing; the code's default is `off` |
| `tenancy.allowInertRls` | `GENUS_TENANT_SWITCH_ALLOW_INERT_RLS` | unset, which renders nothing; the code's default is `false` |

Both render nothing until set, so a release that sets neither renders exactly
as before. Any other switch mode fails at template time, because the code
would read it as `off` and silently disable switching. `allowInertRls` lets
switching work while row-level security is inert, which leaves each query's
own `WHERE` clause as the only tenant boundary. Turn it on deliberately, if at
all.

**Shared vault key.** Credentials in the vault are encrypted with one master
key, and a credential the bridge stores while acting in a child tenant is read
back by that child's engine. So the bridge, the root engine and every cell
must hold the same key. Without one, each process looks for
`<workspace>/.vault-key`. The bridge has no workspace at all, and each cell has
its own, so nothing would match. The chart therefore refuses to render
`tenants` unless `vaultKey.enabled` is true.

```yaml
vault:
  secrets:
    vault-key: {path: genus-os/production/vault-key}
vaultKey:
  enabled: true
```

The Vault path holds two fields, `key` and `fingerprint-salt`, each 32 bytes
stored base64-encoded because Vault KV holds strings. Where those bytes come
from depends on whether the release already has a vault key, and **the chart
cannot tell the two cases apart**. Check before you write anything:

```sh
kubectl exec deploy/<fullname>-engine --namespace <namespace> -- \
  genus vault list
```

Run it as the root tenant. If it lists any rows, or
`/workspace/.vault-key` exists in the engine pod, you are in case (b).

**(a) No key yet: an empty vault and no `/workspace/.vault-key`.** Generate
both values:

```sh
vault kv put <mount>/<path>/vault-key \
  key="$(openssl rand -base64 32)" \
  fingerprint-salt="$(openssl rand -base64 32)"
```

**(b) An existing key: stored secrets, or a `.vault-key` in the workspace.**
Migrate the key the release already uses. **Do not generate a new one.**
Every secret already stored is encrypted under the existing key, and a new
key would make all of them permanently unreadable. Copy the existing files
into Vault instead. A missing salt only changes how fingerprints print, so
generate one if the workspace has none:

```sh
key="$(kubectl exec deploy/<fullname>-engine --namespace <namespace> -- \
  base64 -w0 /workspace/.vault-key)"
salt="$(kubectl exec deploy/<fullname>-engine --namespace <namespace> -- \
  base64 -w0 /workspace/.fingerprint-salt 2>/dev/null || openssl rand -base64 32)"
vault kv put <mount>/<path>/vault-key key="$key" fingerprint-salt="$salt"
unset key salt
```

Then enable `vaultKey`. The engine reads the same key from
`/etc/genus/vault/.vault-key` from then on, and the workspace copy is no
longer used.

VSO decodes them into raw files in the `<fullname>-vault-key` Secret. The
chart mounts them read-only on the bridge, the root engine and every cell as
`/etc/genus/vault/.vault-key` and `/etc/genus/vault/.fingerprint-salt`, and
sets `GENUS_VAULT_KEY_FILE` and `GENUS_FINGERPRINT_SALT_FILE` to those paths.
The basenames are deliberate: the agent secret-file denylist refuses
`.vault-key` and `.fingerprint-salt` wherever they live, so an agent's shell
or file tools cannot read the master key. The mount
is not optional: under those variables a missing file is a hard error, and the
pod waits for the Secret rather than starting without a key. Rotating either
field through VSO restarts the bridge and every engine. Rotating `key`
re-keys nothing, though: secrets already stored stay encrypted under the old
key and become unreadable, so treat `key` as write-once.

The fingerprint salt is separate from the key. A fingerprint (`b2:…`) is
keyed by the salt file, not by the vault key, so two processes with the same
key but different salts print different fingerprints for the same
credential. Sharing the salt as well keeps fingerprints comparable across the
bridge and every engine. It has no effect on whether a secret can be
decrypted.

With `vault.enabled: false` (local values), create the
`<fullname>-vault-key` Secret yourself with the raw bytes under `key` and
`fingerprint-salt`.

**Defaults and inheritance.**

| Key | Default | Notes |
|-----|---------|-------|
| `id` | required | Lowercase DNS label, unique, and not `global.rootTenant`. |
| `replicaCount`, `resources`, `secretRefs`, `podLabels`, `podAnnotations`, `nodeSelector`, `tolerations`, `affinity` | the `engine.*` value | Replaced, not merged. `secretRefs` keeps the engine's allow-list, plus the cell's own `engine-providers-<id>` (see below). |
| `env` | `engine.env` | Merged over it, key by key. On a persistent, writable workspace the chart sets `ROBOTHOR_PLUGIN_DIR` and `PYTHONPATH` (see below); the entry's `env` overrides either. |
| `workspace.allowEmptyFleet`, `minAgentCount`, `requiredAgentIds`, `readOnly` | the top-level `workspace.*` value | |
| `workspace.persistence` | `workspace.persistence` without `existingClaim` | Merged key by key. A cell never mounts the root engine's claim. |
| `workspace.configMap` | none | Never inherited: a bundle holds one tenant's fleet. |
| `extraVolumes`, `extraVolumeMounts` | none | Never inherited: the root engine's extra volumes name its own resources, and a `ReadWriteOnce` claim mounted by a second pod would leave the cell unschedulable. Same shape as `engine.extraVolumes` (see [Extra volumes](#extra-volumes)). |
| `googleWorkspace.enabled` | `false` | Never inherited: credentials belong to one mailbox. |
| `googleWorkspace.secretName` | `<fullname>-engine-<id>-google` | A Secret with a `credentials.json` key, supplied by the deployment. VSO only materializes the root engine's `engine-google`. The mount is optional, as on the root engine. |

**Seeding a cell's workspace.** Same options as the root engine (see
[Workspace and readiness](#workspace-and-readiness)):

- a chart-created PVC (the default when `workspace.persistence.enabled`),
  seeded by an operator or a provisioning job, with the cell unready until a
  valid fleet is present unless `allowEmptyFleet` is true;
- `workspace.persistence.existingClaim`: a volume you have already populated;
- `workspace.configMap`: an immutable bundle, with
  `workspace.persistence.enabled: false` on that cell.

**Plugins in a cell.** Plugins load from `ROBOTHOR_PLUGIN_DIR`, which must
also be on `PYTHONPATH` (see `docs/PLUGINS.md`). The container's root
filesystem is read-only, so a cell keeps its plugins on its own workspace.
When the cell's workspace is persistent (a chart-created PVC or
`existingClaim`) and writable, the chart sets both variables to
`<workspace.mountPath>/.plugins`, so a plugin installed into a cell survives
its restarts and is never visible to another cell. That default wins over
`engine.env`, whose values describe the root engine's workspace; the entry's
own `env` overrides either variable, for example to add a second directory to
`PYTHONPATH`. A cell on an `emptyDir`, a ConfigMap bundle or a `readOnly`
workspace gets neither variable: an install there would be lost on restart, or
could not be written at all. The root engine renders exactly as before, so a
deployment that sets these variables in `engine.env` keeps doing so.

The plugin directory is on the workspace, which the cell's agents can write
to, and `PYTHONPATH` comes before site-packages. A file an agent writes there
(a `sitecustomize.py`, or a package shadowing a real dependency) runs inside
the engine process on its next restart, with every secret the cell reads.
Give the cell's agents a `write_path_allowlist` in their manifests that leaves
out `.plugins/`, with `write_path_restrict` in their `guardrails` list, which
the allowlist needs before it is enforced. To turn
the plugin directory off for a cell, set both variables to empty in its
`env`, which installs into the image's own environment as on a default root
engine:

```yaml
tenants:
  - id: child-a
    env: {ROBOTHOR_PLUGIN_DIR: "", PYTHONPATH: ""}
```

State a plugin keeps outside the workspace goes on a volume the entry declares
itself in `extraVolumes` and `extraVolumeMounts`: a cell never inherits the
root engine's, so give each cell its own claim.

```yaml
tenants:
  - id: child-a
    workspace:
      persistence: {enabled: true}
    extraVolumes:
      - name: plugin-state
        persistentVolumeClaim: {claimName: child-a-plugin-state}
    extraVolumeMounts:
      - {name: plugin-state, mountPath: /var/lib/plugin-state}
```

**Provider credentials per cell.** A cell reads the root engine's provider
class `engine-providers` by default. To give it credentials of its own, add a
class named `engine-providers-<id>` to `vault.secrets` and to that cell's
`secretRefs`, which replace the engine's list wholesale:

```yaml
vault:
  secrets:
    engine-providers-child-a: {path: genus-os/production/engine-providers-child-a}
tenants:
  - id: child-a
    secretRefs: [database, cache, auth-signing, engine-providers-child-a]
```

Only the cell whose `id` the class names may read it. A cell naming another
cell's `engine-providers-<other id>` fails at template time, and so does the
root engine naming any of them in `engine.secretRefs`. The class must exist
in `vault.secrets`, like every other one. Rotating it through VSO restarts
that cell alone. A cell also restarts when any class it names in its own
`secretRefs` rotates, as well as with every class the root engine reads.

**Removing a cell.** Removing an entry from `tenants:` deletes that cell's
Deployment and Service and never deletes its workspace claim. The
chart-created PVC carries two sets of opt-outs:

- `helm.sh/resource-policy: keep`, so `helm upgrade` and `helm uninstall`
  leave it in place;
- `argocd.argoproj.io/sync-options: Prune=false,Delete=false`, because Argo CD
  ignores Helm's policy. `Prune=false` stops `argocd app sync --prune` from
  removing it, and `Delete=false` stops a cascading delete of the Argo CD
  Application from removing it.

Other sync options set in `workspace.persistence.annotations` are kept, and a
`Prune=` or `Delete=` set there is replaced. Under Argo CD the orphaned claim
then shows as requiring pruning, and the Application stays OutOfSync until the
claim is gone. Adding the same `id` back mounts the same workspace again.
Otherwise, delete the claim by hand once you no longer need its contents:
`kubectl delete pvc <fullname>-workspace-<id> --namespace <namespace>`.

## Google Workspace credentials

The engine's mail, calendar and chat tools (`gws_*`) run the `gws` CLI, which
the image ships at a pinned version (`Dockerfile.python`, the `gws` stage). The
CLI needs an authorized-user OAuth file for the mailbox the assistant uses:

| Value | Default | Meaning |
|-------|---------|---------|
| `engine.googleWorkspace.enabled` | `false` | Mount the `engine-google` Secret's `credentials.json` read-only at `/etc/genus/gws/` and set `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` to it. `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` goes to `/app/.cache/gws`, on the writable `runtime-cache` volume, because the CLI rewrites a token cache and the root filesystem is read-only. |

```yaml
vault:
  secrets:
    engine-google: {path: genus-os/production/engine-google}
engine:
  googleWorkspace:
    enabled: true
```

Enabling it without `vault.secrets.engine-google` fails at template time. The
mount itself is `optional`: until the Secret exists the engine starts normally
and the `gws_*` tools return an `auth` error, so turning the switch on before
the credentials are written cannot hold the engine in `ContainerCreating`. VSO
restarts the engine when the credentials rotate.

**Minting the file.** In a Google Cloud project owned by the Workspace, enable
the Gmail API (and Calendar or Chat if those tools are used), set the OAuth
consent screen to **Internal** (an External app in testing mode expires refresh
tokens after seven days), and create an OAuth client of type *Desktop app*.
Then, on a workstation, with the same `gws` version the image pins:

```sh
gws auth login            # sign in as the assistant's mailbox, grant the scopes
gws auth export --unmasked > credentials.json
vault kv put <mount>/<path>/engine-google credentials.json=@credentials.json
rm credentials.json
```

Set `ROBOTHOR_AI_EMAIL` to the same mailbox in `engine-providers`: the reply
tools use it to tell the assistant's own address apart from the people on a
thread. Check from the pod with
`gws gmail users getProfile --params '{"userId":"me"}'`.

**Egress.** `gws` calls `oauth2.googleapis.com` (token refresh),
`gmail.googleapis.com`, `www.googleapis.com` (Calendar) and
`chat.googleapis.com` (Chat) on 443. It loads each API's discovery document
from that same host and caches it under `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`.
`networkPolicy.egress.destinations.https` matches CIDRs, not hostnames, and
Google's API ranges are large and move, so in practice put these hosts in the
egress proxy's allowlist, set `HTTPS_PROXY` in `engine.env`, and allow the
proxy's CIDR in `destinations.https`. A NetworkPolicy that drops the packets
shows up as the `timeout` hint; a proxy that refuses a host usually answers at
once and shows up as `auth` (a 403) or `unclassified`. A `Mail service not enabled` error
instead means the mailbox has no Gmail license in the Workspace admin console;
the credentials are fine and start working once Gmail is on.

## Extra volumes

Each Python workload can mount additional volumes, rendered verbatim with
`toYaml` into the same lists as the chart's own. The order within a list is
not part of the contract (on the engine they follow `workspace`), so refer to
entries by `name`, not by position:

| Value | Default | Meaning |
|-------|---------|---------|
| `engine.extraVolumes` | `[]` | Raw Kubernetes `volume` objects added to the engine pod's `spec.volumes`. |
| `engine.extraVolumeMounts` | `[]` | Raw Kubernetes `volumeMount` objects added to the engine container's `volumeMounts`. |
| `bridge.extraVolumes` / `bridge.extraVolumeMounts` | `[]` | The same, for the bridge. |
| `orchestrator.extraVolumes` / `orchestrator.extraVolumeMounts` | `[]` | The same, for the orchestrator. |

```yaml
engine:
  extraVolumes:
    - name: plugin-state
      persistentVolumeClaim:
        claimName: plugin-state
  extraVolumeMounts:
    - name: plugin-state
      mountPath: /var/lib/plugin-state
```

Names must not collide with the chart's own volumes (`workspace`, `tmp`,
`runtime-cache`, `db-ca`, `migration-api-token`, `google-workspace`), and the chart does not create
the claim. The bridge and orchestrator run several replicas with rolling
updates, so a `ReadWriteOnce` claim belongs on the single-replica engine only.

## Database migration prerequisites

The migration Job uses the sole packaged, checksummed migration manifest
(`robothor migrate --check` prints how many migrations it holds). It takes an
advisory lock, records full IDs and checksums, and refuses drift or unknown
history. Upgrade safeguards preserve legacy memory tables as archives and
require a 30-day replacement-data gate plus a full-row archive before removing
legacy score columns.

The Job's name hashes its whole rendered pod template: image, command,
resources, environment, `database.tls`, labels (so every chart or app version
bump), ServiceAccount, security context, scheduling and pull secrets. A Job's
pod template is immutable, so any change there produces a new Job that runs
once; an upgrade that changes none of it keeps the completed Job and does not
re-run it. Rotating the CA inside its ConfigMap changes no part of the
template, so it does not rename the Job: kubelet refreshes the mounted file
and libpq reads it on the next connection.

The chain requires PostgreSQL extensions `vector`, `uuid-ossp`, `citext`, and
`pgcrypto`. Managed services must pre-provision them or grant the migration
identity sufficient extension permission. Before rollout, run the full chain
against a production clone, compare material row counts, take and verify an
encrypted snapshot, and rehearse restore. The migration chain is forward-only;
rolling back an image does not reverse schema or external side effects.

Production and staging set PostgreSQL `sslmode=verify-full`; the database
endpoint must present a certificate signed by a CA the clients trust. Supply
that CA as a ConfigMap and name it in `database.tls`:

| Value | Default | Meaning |
|-------|---------|---------|
| `database.tls.caConfigMap` | `""` | ConfigMap in the release namespace holding the PEM CA bundle that signs the database server certificate. Empty: nothing is mounted and libpq's default `root.crt` lookup applies. |
| `database.tls.caKey` | `ca.pem` | Key in that ConfigMap. |
| `database.tls.allowDefaultTrustStore` | `false` | Explicit opt-out of the render-time check below, for an environment whose pods really provide libpq's default `root.crt`. |

When set, engine, bridge, orchestrator and the migration Job mount the key
read-only at `/etc/genus/db-ca/<caKey>` and set `PGSSLROOTCERT` to it. In any
environment, `sslMode: verify-ca` or `verify-full` without `caConfigMap` fails
at render time unless `allowDefaultTrustStore: true` says the default trust
store is intended. The check never depends on the environment's name. See
[Trusting the database's CA](../../docs/deployment.md#trusting-the-databases-ca).

Production and staging NetworkPolicies default-deny ingress and egress, then
rebuild only the declared component graph: the dashboard calls engine, bridge,
orchestrator and, when the in-chart cache is enabled, the cache port; the engine
calls bridge, orchestrator and NATS; the bridge calls the engine's admin API (at
`ROBOTHOR_ENGINE_URL`, which the chart points at the engine Service whenever
`engine.enabled`) and the orchestrator. In-chart PostgreSQL/Redis are selected by
pod labels; the cache's ingress admits the Python services and the dashboard, and
nothing else. Managed
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
# Must be NON-EMPTY while valkey.auth.enabled is true (the default). The
# subchart hashes whatever it finds, so an empty value becomes
# `user default on #<sha256 of "">` in the ACL while clients with an empty
# password send no AUTH at all — every connection then fails NOAUTH, with
# nothing failing at install time. Either set a real value here or turn the
# server's auth off with `valkey.auth.enabled: false`, as values-local.yaml does.
kubectl -n genus create secret generic gos-genus-os-cache \
  --from-literal=ROBOTHOR_REDIS_PASSWORD='local-only-cache-password'
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
