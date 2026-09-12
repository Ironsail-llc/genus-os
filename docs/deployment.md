# Deployment

One installer, three places to put an instance:

| Where | How you get there | This page |
|-------|-------------------|-----------|
| Containers on one box | `genus init --substrate compose` | [Docker Compose](#docker-compose) |
| The host, with systemd units | `genus init` (the `local` substrate), then `scripts/install-units.sh` | [Systemd services](#systemd-services) |
| Kubernetes | The `helm/genus-os` chart | [Helm](#helm) |

The wizard, the setup link and the doctor are the same on all three; the
[quick start](quickstart.md) is where an operator starts. This page is what
comes after: upgrades, unit installation, secrets backends, the chart's
readiness contract and the full diagnostic inventory.

## Docker Compose

Two compose files make the whole platform, and two overlays adjust it:

| File | What it carries |
|------|-----------------|
| `infra/docker-compose.yml` | PostgreSQL (pgvector), Redis, Ollama, plus the optional monitoring/TTS/media/tunnel profiles |
| `infra/docker-compose.apps.yml` | The release stack: `migrate`, `engine`, `bridge`, `orchestrator`, `dashboard`, all from GHCR |
| `infra/docker-compose.gpu.yml` | Ollama's NVIDIA reservation. Add it only where the NVIDIA container runtime works — it fails the whole stack where it does not |
| `infra/docker-compose.dev.yml` | Source bind mounts and local builds, for working on Genus OS itself |

### The short way

`genus init --substrate compose` does everything below — writes the env file,
picks the overlays, starts the stack, waits for all four `/ready` endpoints and
prints a single-use `/setup` link. See the [quick start](quickstart.md).

### By hand

```bash
# One directory holds the compose files, the env file and the workspace.
mkdir -p "$HOME/genus/workspace" && cd "$HOME/genus"
curl -fsSLO https://raw.githubusercontent.com/Ironsail-llc/genus-os/main/infra/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/Ironsail-llc/genus-os/main/infra/docker-compose.apps.yml

# Every credential, in one file nothing else may read. The paths inside it
# must be the paths you are standing in: compose resolves each service's
# env_file against them, and dockerd would create a missing bind-mount source
# as a root-owned directory the containers cannot write.
cat > genus.env <<ENV
GENUS_IMAGE_TAG=v1.69.2
GENUS_WORKSPACE=$HOME/genus/workspace
GENUS_ENV_FILE=$HOME/genus/genus.env
GENUS_UID=$(id -u)
GENUS_GID=$(id -g)
ROBOTHOR_DB_NAME=robothor_memory
ROBOTHOR_DB_USER=robothor
ROBOTHOR_DB_PASSWORD=choose-a-password
ROBOTHOR_SECRETS_BACKEND=env
AUTH_SECRET=$(openssl rand -base64 32)
GENUS_BRIDGE_SSO_SECRET=$(openssl rand -base64 32)
GENUS_LOCAL_LOGIN=true
OPENROUTER_API_KEY=sk-your-key
ENV
chmod 600 genus.env

docker compose --env-file ./genus.env \
  -f docker-compose.yml -f docker-compose.apps.yml up -d
```

`AUTH_SECRET`, `GENUS_BRIDGE_SSO_SECRET` and one sign-in method are not
optional: the dashboard's `/api/ready` reports unhealthy without all three, so
the stack never finishes coming up. `genus init --substrate compose` generates
them for you.

`docker compose config` prints this file's contents in full, including the
database password and the provider key. Redirect it to a file only you can
read, or do not redirect it at all.

Three variables configure the compose FILE rather than the platform, so none of
them is a declared setting and `genus config` does not know them:

- **`GENUS_IMAGE_TAG`** — required, with no default. The release build publishes
  `vX.Y.Z`, `vX.Y`, `vX` and `sha-<short>` and deliberately no `latest`, so a
  default here would name a tag that does not exist; compose refuses to start
  instead, naming the variable.
- **`GENUS_WORKSPACE`** — the host directory holding this instance's own
  `brain/`, `docs/agents/` and `.robothor/` (instance data, created by
  `genus init`), mounted at `/workspace`. Create it before `up`: a missing bind
  source is created by dockerd as root.
- **`GENUS_UID` / `GENUS_GID`** — the account the python services run as
  (default 1000, the image's own user). Everything in the workspace is 0600 and
  owned by whoever installed, and the bridge must be able to read
  `.robothor/setup_token.yaml` — that file is the only door into a fresh
  instance. `genus init` writes your real uid and gid here and refuses to run
  as root.
- **`GENUS_ENV_FILE`** — the file above. Every service reads it through
  `env_file`; nothing is inlined in the compose file.

The container-side names (`ROBOTHOR_WORKSPACE=/workspace`,
`ROBOTHOR_DB_HOST=postgres`, `ROBOTHOR_OWNER_CONFIG=/workspace/.robothor/owner.yaml`)
are set explicitly per service and therefore outrank the env file. A
host-oriented value cannot point a container back at itself.

### The schema

The `migrate` service runs `python -m robothor.cli migrate` once, and the
engine, bridge and orchestrator start only after it exits 0
(`depends_on: migrate: condition: service_completed_successfully`) — the compose
equivalent of the chart's `wait-for-migrations` init container. Nothing else
creates schema: no SQL is mounted into `docker-entrypoint-initdb.d`, so every
applied file is recorded in the `schema_migrations_v2` ledger with its SHA-256
checksum. A schema created any other way leaves that ledger empty, and a later
upgrade cannot tell it apart from an empty database.

```bash
docker compose --env-file ./genus.env -f docker-compose.yml -f docker-compose.apps.yml ps
docker compose --env-file ./genus.env -f docker-compose.yml -f docker-compose.apps.yml logs migrate
```

### Upgrading

There is no floating tag to chase, so an upgrade is a one-line edit and one
`up -d`. Four steps, in this order:

```bash
# 1. Name the release you are moving to. No `latest` exists to pull by accident.
sed -i 's/^GENUS_IMAGE_TAG=.*/GENUS_IMAGE_TAG=v1.69.2/' genus.env

# 2. Pull it before anything stops, so a bad tag fails while the old stack is up.
docker compose --env-file ./genus.env \
  -f docker-compose.yml -f docker-compose.apps.yml pull

# 3. Reconcile. Compose re-runs the one-shot `migrate` service and holds the
#    engine, bridge, orchestrator and dashboard until it exits 0.
docker compose --env-file ./genus.env \
  -f docker-compose.yml -f docker-compose.apps.yml up -d

# 4. Ask, rather than assume.
genus doctor
```

Step 3 is where the schema moves: `migrate` carries `restart: "no"`, and the
platform services gate on `service_completed_successfully`, so a migration that
fails leaves them on the **old** images rather than starting against a
half-migrated database. Read `logs migrate`, fix, and run `up -d` again — it is
the same command either way.

Nothing here is a rolling upgrade: the services restart. Take a snapshot first
on an instance whose data you care about (`genus snapshot create`), and read
[the schema](#the-schema) before upgrading a database this migrator did not
create.

### Working on Genus OS itself

The dev overlay restores the source bind mounts and the local builds the
release file gave up. It needs no `genus.env` — the release file's `env_file`
entry is optional, so a checkout with nothing but the two variables the base
file interpolates renders and builds:

```bash
docker compose \
  -f infra/docker-compose.yml \
  -f infra/docker-compose.apps.yml \
  -f infra/docker-compose.dev.yml up -d --build
```

### Ollama Model Setup

After Ollama starts, pull the required models:

```bash
# Required: embedding model (639 MB)
docker exec robothor-ollama ollama pull qwen3-embedding:0.6b

# Required: reranker model (1.2 GB)
docker exec robothor-ollama ollama pull Qwen3-Reranker-0.6B:F16

# Optional: generation model (loaded on demand for RAG)
docker exec robothor-ollama ollama pull qwen3:8b

# Optional: vision model (for scene analysis)
docker exec robothor-ollama ollama pull llama3.2-vision:11b
```

### GPU Support

The base file reserves nothing: a `deploy.resources.devices` block fails the
entire stack on a box without the NVIDIA container runtime. Add the overlay
where GPUs work, and nothing where they do not:

```bash
docker compose -f infra/docker-compose.yml -f infra/docker-compose.gpu.yml up -d
```

`genus init --substrate compose` adds it when `nvidia-smi` answers, and says so
in the plan.

## Systemd Services

Run `genus init` first: the units are how an already-initialized instance
survives a reboot, not a second way to install one.

**Do not copy a unit file into `/etc` by hand.** One script renders and installs
every one of them:

```bash
sudo mkdir -p /etc/robothor
sudo cp infra/systemd/robothor.env.example /etc/robothor/robothor.env
sudo chmod 640 /etc/robothor/robothor.env
# Edit /etc/robothor/robothor.env: at minimum ROBOTHOR_SERVICE_USER,
# ROBOTHOR_WORKSPACE and the database settings for this box.

sudo scripts/install-units.sh
sudo systemctl daemon-reload
sudo systemctl enable --now robothor-engine robothor-bridge robothor-app
```

`scripts/install-units.sh` does four things a `cp` cannot:

- **It renders.** Every template goes through `scripts/render-unit.sh`, which
  expands `ROBOTHOR_WORKSPACE`, `ROBOTHOR_SERVICE_USER` and the rest, and
  **refuses** a unit left holding an unexpanded placeholder or a literal `%h`
  (which systemd resolves to `/root` in a system unit — the bug that put an
  agent's workspace in root's home).
- **It verifies.** Each rendered `.service` is gated on `systemd-analyze verify`,
  and nothing is installed unless every one of them rendered.
- **It is idempotent, and it reports.** installed / updated / unchanged, per
  unit, so re-running it after a `git pull` is the supported way to pick up a
  template fix. Hand-edited copies in `/etc` are exactly the drift that made
  repo fixes never reach the box.
- **It installs the pieces that are not units**: the restart handler into
  `/usr/local/lib/robothor/` (root-owned, outside the checkout, so an agent
  cannot rewrite what runs as root) and the `tmpfiles.d` fragments that create
  `/run/robothor`.

It only ever installs `robothor-*` units — never anything instance-local — and
it restarts nothing. `sudo systemctl daemon-reload` is yours to run.

`scripts/instance_doctor.sh` answers the other direction: what is installed on
this box that no template describes. `genus doctor --only host.unit_drift` wraps
it; the findings are catalogued in
`docs/runbooks/INSTANCE_DOCTOR.md` in the repository.

Service features, declared in the templates:
- `Restart=always` with 5s backoff
- `KillMode=control-group` (no orphaned children)
- Security hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`
- `EnvironmentFile=/etc/robothor/robothor.env` plus an optional
  `EnvironmentFile=-/run/robothor/secrets.env`
- `Requires=robothor-secrets.service` on every service that needs a credential

View logs: `journalctl -u robothor-engine -f`

### The sign-in secrets the units need

`genus init` mints `AUTH_SECRET` and `GENUS_BRIDGE_SSO_SECRET` into the
workspace's `genus.env` (mode 0600). `genus doctor` reads that file from the
workspace, and so does the wizard's own `verify` step — but **systemd does
not**, because the units read `/etc/robothor/robothor.env`. Point them at it
once, with a drop-in:

```bash
sudo systemctl edit robothor-bridge
# [Service]
# EnvironmentFile=-/home/robothor/robothor/genus.env
```

or copy the two lines into `/etc/robothor/robothor.env`. Without them the
bridge refuses every `/api/auth/sso` exchange and nobody can sign in, while
`/ready` stays green — the failure mode that kept `app.robothor.ai` locked out
for eight days. `genus doctor`'s `secrets.bridge_sso` check is the one that
says so.

### Secrets backends

`robothor-secrets.service` runs `scripts/load-secrets.sh` before the other
services start, and every service that needs a credential loads the file it
writes (`/run/robothor/secrets.env`, tmpfs, mode 0600). `ROBOTHOR_SECRETS_BACKEND`
in `/etc/robothor/robothor.env` chooses where that file comes from:

| Backend | Where the secrets live | What the loader does |
|---|---|---|
| `env` | `/etc/robothor/robothor.env`, a unit drop-in, or the container environment | writes an **empty** `secrets.env`, so each consumer's `EnvironmentFile=` resolves |
| `file` | a plaintext `KEY=VALUE` file you manage (`ROBOTHOR_SECRETS_BACKEND_FILE`, default `/etc/robothor/secrets.env`) | validates it and copies it to `secrets.env` at mode 0600 |
| `sops` | `/etc/robothor/secrets.enc.json`, encrypted to the age key at `/etc/robothor/age.key` | runs `scripts/decrypt-secrets.sh` |

Leave `ROBOTHOR_SECRETS_BACKEND` unset and the loader **auto-detects**: `sops`
if the encrypted store exists, otherwise `file` if the plaintext file exists,
otherwise `env`. It prints the backend it chose on one line, so
`journalctl -u robothor-secrets` says which path a boot took. Nothing about an
existing SOPS instance changes.

**SOPS is opt-in.** It used to be a prerequisite: the unit carried
`ConditionPathExists=/etc/robothor/secrets.enc.json` and the engine's
`ExecStartPre` ran the decrypt directly, so an operator without an age key could
not start the platform at all.

The `file` backend refuses anything it should not load, and says why in one
line:

- it must be a **regular file** at the configured path,
- its mode must be **0600 or 0400** — anything group- or world-readable exposes
  every credential the instance owns,
- it must be owned by **root or the service account** — otherwise a third party
  can rewrite what the platform loads into every service's environment.

Failures reach a human rather than degrading quietly: the loader exits non-zero,
`robothor-secrets.service` has `OnFailure=robothor-alert@%n.service`, and its
four consumers `Requires=` it. Nothing retries a `oneshot`, so a failed load is
a four-service outage that will not heal itself — see `infra/systemd/README.md`
for the recovery.

In Python, read a credential through the one accessor rather than `os.environ`:

```python
from robothor.secrets import get_secret, secret_source

get_secret("OPENROUTER_API_KEY")      # value, or None
secret_source("OPENROUTER_API_KEY")   # "env" | "vault" | "missing" — safe to print
```

It walks the process environment (whatever the backend put there) and then the
encrypted vault (`genus vault set …`), and treats an unreadable vault as "not
configured" rather than raising.

## Preparing the dependencies by hand

The `local` substrate needs PostgreSQL and Redis running before `genus init`
will plan. This is how you get them there — it is not a fourth way to install
the platform.

### PostgreSQL

```bash
# Install pgvector extension
sudo apt install postgresql-16-pgvector  # Debian/Ubuntu

# Create the database and the roles. No schema is created here.
sudo -u postgres createdb robothor_memory
sudo -u postgres psql -d robothor_memory -c "CREATE EXTENSION vector"
sudo -u postgres psql -d robothor_memory -c "CREATE EXTENSION \"uuid-ossp\""
sudo -u postgres psql -c "CREATE USER robothor WITH PASSWORD 'your-password'"
sudo -u postgres psql -c "GRANT ALL ON DATABASE robothor_memory TO robothor"
sudo -u postgres psql -d robothor_memory -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO robothor"
sudo -u postgres psql -d robothor_memory -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO robothor"
```

**Never hand a SQL file to `psql` to create the schema.** The only thing that
creates tables is the migrator — `genus init` runs it, the compose stack runs it
as a one-shot service, and you can run it yourself:

```bash
genus migrate
genus migrate --status
```

It reads `ROBOTHOR_DB_HOST`, `ROBOTHOR_DB_PORT`, `ROBOTHOR_DB_NAME`,
`ROBOTHOR_DB_USER` and `ROBOTHOR_DB_PASSWORD`, applies the whole ordered
manifest, and records each file in the `schema_migrations_v2` ledger with its
SHA-256 checksum. A schema created any other way leaves that ledger empty, and a
later upgrade cannot tell it apart from an empty database.

If you are upgrading a database whose schema this migrator did not create — one
seeded by an SQL file mounted into `docker-entrypoint-initdb.d`, or by a
pre-ledger upgrade path — it refuses to run rather than replay migrations over
your data. Replaying is not harmless: `019_unified_session.sql` deletes chat
sessions, `035_drop_legacy_buddy_columns.sql` aborts mid-chain.

Adopt the history once. This records the baseline, plus every migration named in
the legacy `.robothor/migrations_applied.yaml` side-ledger, as applied *without
executing them*, then applies whatever genuinely remains:

```bash
genus migrate --adopt-baseline
```

Keep `.robothor/migrations_applied.yaml` until this has run — its `migrations:`
list is the only record of what the pre-ledger path applied, and it is retired
on its own once `schema_migrations_v2` covers every entry it names.
`genus migrate --status` shows the provenance of each row.

If that side-ledger is gone and the schema is past the baseline, `--adopt-baseline`
refuses a second time: adopting only `001_init` and then running everything after
it would be the same replay. Say how far the schema actually got instead:

```bash
genus migrate --status  # spell the id exactly; do not trust the applied/pending
                        # split here — reconciled rows are hearsay
genus migrate --adopt-through 040_memory_episodes
```

`--adopt-through <migration_id>` adopts every migration up to and including that
id without executing any of them, then applies the rest normally. It implies
`--adopt-baseline`, so it can be used on its own.

**Pick the id from the schema, not from the status output.** On a database in
this state the ledger's "applied" rows were copied from the legacy
`schema_migrations` table, which `018_migration_tracking.sql` backfills as far
as 018 regardless of what actually ran — so `--status` will happily suggest an
id that is nowhere near where the schema really is. Check which tables and
columns exist instead.

Adoption is effectively irreversible: undoing it means hand-editing
`schema_migrations_v2`. And a migration skipped by too late an id is recorded as
applied with a valid checksum, so it will never be reported as missing or as
drift — it becomes a permanent, silent gap in the schema. Err early: name the
last id you are *certain* of and let the remaining migrations apply, since
re-applying a migration whose objects already exist is the case those files are
written to tolerate.

### Redis

```bash
sudo apt install redis-server
# Set maxmemory in /etc/redis/redis.conf:
#   maxmemory 2gb
#   maxmemory-policy allkeys-lru
sudo systemctl enable --now redis-server
```

### Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3-embedding:0.6b
ollama pull Qwen3-Reranker-0.6B:F16
```

## Running `genus init` on a server

`genus init` is two phases. The first runs every step's check and prints the
plan; the second applies it, recording each finished step in
`<workspace>/.robothor/init_state.yaml` so an interrupted install resumes at the
next step rather than the first. If a **required** check fails, phase 2 never
starts: nothing is written and the command exits 1 naming the check. That is
what makes it safe to put in a provisioning script — it either produces a
running instance or changes nothing.

For an unattended install, give it everything up front:

```bash
export ROBOTHOR_DB_HOST=db.internal.test
export ROBOTHOR_DB_PASSWORD="$DB_PASSWORD"
export OPENROUTER_API_KEY="$PROVIDER_KEY"
genus init --yes --json \
  --owner-name "Ada Lovelace" --owner-email ada@example.com \
  --preset standard --secrets-backend sops
```

`--json` puts one document on stdout — `plan`, `steps`, `first_run_url` and
`exit_code` — with the human narration on stderr. No credential appears in
either stream; the setup token appears exactly once, inside `first_run_url`,
which is the point of it.

What the wizard writes, and where:

| What | Where | Why there |
|------|-------|-----------|
| Operator identity | `~/.robothor/owner.yaml` | The file the platform reads. Never `.env` — two files naming one operator is how an instance answers to one name and files CRM rows under another. |
| Settings (database, provider model, secrets backend) | `<workspace>/.robothor/config.yaml` | Written through the same writer `genus config set` uses. |
| Workspace pointer | `<workspace>/.env` | `ROBOTHOR_WORKSPACE` only — config.yaml lives inside the workspace and cannot say where it is. |
| Provider key, Telegram token | The instance vault | Secrets never go in config.yaml, which gets copied into bug reports. |
| Resume state | `<workspace>/.robothor/init_state.yaml` | One `step: completed` line each. |

Re-running is routine. An existing `owner.yaml` is never overwritten, and
completed steps are skipped — except the security note, the verification and the
link, which run every time, because each one mints or checks something that must
be current.

## First run on a headless box

A fresh instance has no operator account, so the dashboard's sign-in page has
nothing to render. The way in is the first-run wizard at `/setup`, reached with
a single-use token.

`genus init` prints that link at the end of an install. On a server you were not
watching — a container, a provisioning script, someone else's terminal — mint a
fresh one:

```bash
genus auth setup-link
```

It prints a URL carrying a token that is good once, for 30 minutes
(`GENUS_SETUP_TOKEN_TTL_SECONDS`). `--ttl 2h` widens the window for an install
you have to walk away from; `--json` is for a provisioning script. The command
is deliberately local-only: minting requires shell access to the box, which is
the one credential the wizard's gate can rely on before an account exists.

The printed address is `127.0.0.1` because that is where the dashboard binds.
That is loopback on the **server**, so forward the port rather than exposing it:

```bash
ssh -L 3004:127.0.0.1:3004 box.example.test
```

Then open the printed link on your own machine. `genus init` prints this line
for you whenever it is not running on a terminal, or the dashboard is not on
loopback.

### Rate limiting the setup link

`POST /api/setup/claim` allows five attempts a minute per client address, and
the dashboard calls the bridge server-side — so unless the deployment says which
hop is its own edge, every claim on the appliance shares one bucket keyed on the
dashboard. Anyone who can reach `/setup` could then spend that quota
continuously and keep the real operator's claim answering 429 for the life of
their token.

Two allowlists have to agree before an address is believed, exactly as for
`POST /api/auth/login`:

```bash
GENUS_DASHBOARD_TRUSTED_PROXIES=10.42.0.7/32   # what the dashboard will vouch for
GENUS_TRUSTED_PROXIES=10.42.0.7/32             # whose X-Client-IP the bridge honours
```

Prefer a `/32` over a pod CIDR, which would cover every workload in the
namespace. With either unset nothing is asserted and the bridge falls back to
its own peer address — safe, but the limiter is then appliance-wide.

Once the wizard finishes — that is, the moment an operator account exists —
`/setup` and every route behind it answer 404, and `genus auth setup-link`
refuses to mint another. The completion signal is the owner row in the
database, not a file, so deleting anything on disk does not reopen it. From
then on the door is the sign-in page; a forgotten password is
`genus user set-password <email>`.

## Helm

The chart is `helm/genus-os`. It brings its own PostgreSQL and Redis
subcharts (`postgres.enabled`, `redis.enabled`) so a cluster with neither can
still render, and splits secret material into independently rotatable classes —
database, cache, signing, SSO/OIDC, dashboard, provider — with per-component
references enforced. The dashboard and the migration job cannot request the
privileged or provider classes.

```bash
helm repo add groundhog2k https://groundhog2k.github.io/helm-charts/
helm dependency update helm/genus-os

helm install gos helm/genus-os \
  --namespace genus --create-namespace \
  --values helm/genus-os/values.yaml \
  --values helm/genus-os/values-local.yaml \
  --set vault.enabled=false

helm test gos --namespace genus
```

`values.yaml` carries the chart defaults; `values-local.yaml` is for
minikube/kind (Vault off, secrets created with `kubectl create secret`);
`values-staging.yaml` and `values-production.yaml` are the real environments,
and an infrastructure repository may layer a fourth file on top — it may narrow
a secret class or a NetworkPolicy, never collapse one. An upgrade is
`helm upgrade` with the same values; the release tag in
`values-production.yaml` is what semantic-release bumps and ArgoCD syncs.

### Why `/ready` waits for an agent manifest

Production sets three values that the chart turns into engine environment
variables — the same three the compose release file sets:

| Value | Production | Environment variable |
|-------|------------|----------------------|
| `workspace.allowEmptyFleet` | `false` | `ROBOTHOR_ALLOW_EMPTY_FLEET` |
| `workspace.minAgentCount` | `1` | `ROBOTHOR_MIN_AGENT_COUNT` |
| `workspace.requiredAgentIds` | `[main]` | `ROBOTHOR_REQUIRED_AGENT_IDS` |

The engine's `GET /ready` loads every manifest in the workspace's manifest
directory and checks that set against `requiredAgentIds`. A manifest that will
not parse is reported **broken** (never absent), and a required id that is
missing makes readiness fail outright. So on staging and production the pod
stays unready until the instance manifest `docs/agents/main.yaml` exists in the
mounted workspace **and parses** — a fresh PVC starts unready on purpose,
because an engine with no fleet is not something to route traffic at.

Two ways to satisfy it:

- mount a PVC (`workspace.persistence.enabled: true`) and seed it — a
  provisioning job that runs `genus init`, or a restored snapshot;
- mount an immutable workspace from a ConfigMap
  (`workspace.configMap.name` + `items`), which must include
  `docs/agents/main.yaml` (instance data) and every instruction file it names.

Manifests are instance data: `docs/agents/` is gitignored, and the fleet an
instance runs is the operator's, not the platform's.

### Seeding a fleet

The wizard installs a named preset, and so can you afterwards — one code path
either way:

```bash
genus agent catalog                      # what the presets contain
genus agent install --preset standard
```

`minimal` is three agents and nothing scheduled, `standard` adds email triage,
calendar watch and briefings, `full` is the whole catalogue. `genus init
--preset NAME` does it during an install.

## Production Checklist

- [ ] Set a strong `ROBOTHOR_DB_PASSWORD`
- [ ] PostgreSQL: tune `max_connections` (recommended: 200), `shared_buffers`, `work_mem`
- [ ] PostgreSQL: enable SSL for remote connections
- [ ] Redis: set a password if exposed beyond localhost
- [ ] Redis: set `maxmemory` and `appendonly yes` for durability
- [ ] Ollama: verify GPU access with `ollama run qwen3-embedding:0.6b`
- [ ] Run `genus doctor` and clear every `required` failure (exit 0)
- [ ] Run `genus migrate --status` and confirm no drift and nothing pending
- [ ] Set up log rotation for `/var/log/robothor/`
- [ ] Rebuild vector indexes after initial data load: `REINDEX INDEX idx_facts_embedding`
- [ ] Set `EVENT_BUS_ENABLED=true` if using the event bus
- [ ] Create `agent_capabilities.json` if deploying multiple agents
- [ ] Create `robothor-services.json` for service registry
- [ ] Set up monitoring (health endpoints return JSON)
- [ ] Back up PostgreSQL daily (`pg_dump robothor_memory`)

## Diagnostics (`genus doctor`)

`genus doctor` is the single answer to "is this instance actually working?".
It replaces `genus config validate`, which still works and now prints a
deprecation note and runs `genus doctor --offline` — the free checks only, so a
runbook that still types it does not start spending provider budget. Its
`--json` shape is the doctor's; see
[Configuration](configuration.md#validate-is-now-an-alias-for-genus-doctor).

```bash
genus doctor                        # everything, as a table
genus doctor --json                 # the same report, machine-readable
genus doctor --only db.migrations   # one check
genus doctor --category secrets     # one category
genus doctor --offline              # nothing that costs money, leaves the box, or forks
genus doctor --fix                  # repair what can be repaired
genus doctor --fix --dry-run        # say what --fix would repair
```

### What it checks

| Category | Checks |
|----------|--------|
| `config` | settings resolve; no unknown keys in `config.yaml`; no deprecated names in use; nothing waiting on a restart |
| `database` | PostgreSQL reachable; migrations applied and undrifted; the `service` RBAC role is seeded |
| `redis` | Redis answers a PING |
| `models` | a provider credential resolves; the fleet's default model answers a one-token completion; Ollama is reachable |
| `channels` | Telegram is configured consistently (and `getMe` answers); a Slack bot token, if set, is shaped like one |
| `services` | engine, bridge, orchestrator and vision answer a health endpoint |
| `manifests` | every agent manifest satisfies the schema; none is present-but-unreadable |
| `identity` | an operator is configured in `owner.yaml`; an owner account exists so somebody can sign in |
| `secrets` | the secrets backend is usable; the JWT signing key resolves; the bridge SSO secret is set where auth is enforced |
| `host` | the installed systemd units match the repo (wraps `scripts/instance_doctor.sh`) |

Installed plugins may add their own checks through the `genus.doctor`
entry-point group. A plugin's check ids must be prefixed with its distribution
name and may not shadow a built-in; a plugin that breaks either rule has all of
its checks skipped and the refusal logged.

### Severities

| Severity | Meaning |
|----------|---------|
| `required` | the instance does not work. Exit code 1 |
| `recommended` | it works, but something an operator would want is missing or drifting. Reported, never fatal |
| `info` | recorded for the record; not a verdict |

A check that could not run — no credential, no systemd, Ollama not configured —
is reported as `skip` with the reason. A skip is never a pass.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | no required check failed |
| 1 | a required check failed |
| 2 | the doctor itself could not run (an unknown `--only` id, a registry that would not import) |

2 is deliberately separate from 1: "nothing is wrong" and "nothing was checked"
must never share an exit code, or a typo in a CI gate becomes a permanently
green build.

### What `--fix` covers

Two repairs, both idempotent, both re-run their own check afterwards and report
what the re-run found rather than what the repair claimed:

- `db.migrations` — applies pending migrations (the same work as
  `genus migrate`). Drift and a ledger row this checkout does not ship are
  **not** repairable; they need a human.
- `db.rbac_service_role` — executes migration `107_seed_service_role.sql`.

Everything else reports the command to run. `identity.owner_account` is
deliberately not auto-fixable: minting a privileged account from a diagnostic
that can run on a timer would be a privilege-escalation path, not a repair.

Each check is time-boxed to five seconds (`--timeout S`); one that exceeds it is
reported as a failure and the run continues.

### From the dashboard

`GET /api/doctor` on the bridge serves the same report as `--json --offline`,
gated on the operator role. Its `status` and `checks` keys mirror the readiness
contract so the Helm's Health view renders either payload.

It runs **offline**, under a 30-second budget for the whole run on top of the
per-check five seconds, and its report is **memoised for 30 seconds behind a
single-flight lock** — concurrent polls share one run, and a poll inside the
window reuses the last report rather than running the doctor again. Use the CLI
when you need an answer taken just now. This is what a Health panel polls: online, every
refresh would make a paid completion through the fleet's default model, a call
to Telegram and a fork of the host script, so two operator tabs at 30-second
intervals would be thousands of provider calls a day caused by a dashboard.
The three checks that cost money, leave the box or fork report themselves
`skip` with the reason; run the CLI for those. Checks the total budget does not
reach are reported as **not run**, never as passing.

## Health Endpoints

| Service | Endpoint | Expected |
|---------|----------|----------|
| API Server | `GET /health` on :9099 | `{"status": "ok"}` |
| Bridge | `GET /health` on :9100 | `{"status": "ok"}` |
| Vision | `GET /health` on :8600 | `{"status": "ok", "mode": "..."}` |

## Directory Structure (Production)

```
/opt/robothor/                  # Application code
/etc/robothor/robothor.env      # Configuration (mode 640)
/var/log/robothor/              # Logs
/var/lib/robothor/              # Runtime data (ROBOTHOR_WORKSPACE)
/var/lib/robothor/memory/       # Memory files
/var/lib/robothor/faces/        # Face recognition database
```
