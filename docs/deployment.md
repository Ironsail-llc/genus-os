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

**`GENUS_IMAGE_TAG`** is required and has no default. The release build
publishes `vX.Y.Z`, `vX.Y`, `vX` and `sha-<short>` and deliberately no
`latest`, so a default here would name a tag that does not exist; compose
refuses to start instead, naming the variable. It *is* a declared setting —
`genus config explain GENUS_IMAGE_TAG` works, and it appears in the
[`substrate` group of the reference](reference/configuration.md).

The other four configure the compose FILE rather than the platform, so none of
them is declared and `genus config` does not know them:

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
# 1. Name the release you are moving to -- a real tag from the releases page.
#    There is no `latest` to pull by accident.
sed -i 's/^GENUS_IMAGE_TAG=.*/GENUS_IMAGE_TAG=vX.Y.Z/' genus.env

# 2. Pull it before anything stops, so a bad tag fails while the old stack is up.
docker compose --env-file ./genus.env \
  -f docker-compose.yml -f docker-compose.apps.yml pull

# 3. Reconcile. Compose re-runs the one-shot `migrate` service and holds the
#    engine, bridge and orchestrator until it exits 0 (the dashboard waits
#    on those three being healthy, so it is held one step behind them).
docker compose --env-file ./genus.env \
  -f docker-compose.yml -f docker-compose.apps.yml up -d

# 4. Ask, rather than assume.
genus doctor
```

Step 3 is where the schema moves: `migrate` carries `restart: "no"`, and the
engine, bridge and orchestrator gate on `service_completed_successfully` — and
the dashboard on those three being healthy — so a migration that fails leaves
them on the **old** images rather than starting against a half-migrated
database. Read `logs migrate`, fix, and run `up -d` again — it is
the same command either way.

Nothing here is a rolling upgrade: the services restart. Take a snapshot first
on an instance whose data you care about (`genus snapshot create`), and read
[the schema](#the-schema) before upgrading a database this migrator did not
create.

### Working on Genus OS itself

The dev overlay restores the source bind mounts and the local builds the
release file gave up. It needs no `genus.env` — the release file's `env_file`
entry is optional — but two variables are interpolated with `:?`, so compose
refuses to render without them, and they live in two different files:
`ROBOTHOR_DB_PASSWORD` in the base file, and `GENUS_IMAGE_TAG` in the release
file (any value will do, because the overlay replaces every image it names):

```bash
export ROBOTHOR_DB_PASSWORD=choose-a-password
export GENUS_IMAGE_TAG=dev
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

It only ever installs `robothor-*` units — never anything instance-local. By
default it restarts nothing and prints the follow-up as **one** command:
`daemon-reload`, then a single `systemctl restart` naming the secrets unit and
every service that `Requires=` it. `--restart` runs exactly that. Do not split
it into a secrets restart followed by a services restart — the second restart
supersedes the first one's start jobs and kills their `ExecStartPre`, which
pages (`docs/runbooks/PAGING.md`, "A deploy restarts each unit once").

`scripts/instance_doctor.sh` answers the other direction: what is installed on
this box that no template describes. `genus doctor --only host.unit_drift` wraps
it; the findings are catalogued in
`docs/runbooks/INSTANCE_DOCTOR.md` in the repository.

Service features are declared per template, and they are **not** uniform — read
the unit rather than assuming:

| | engine | bridge | orchestrator | app |
|---|---|---|---|---|
| `Restart=` | `always` | `always` | `on-failure` | `always` |
| `KillMode=` | `control-group` | `control-group` | `mixed` | `control-group` |
| `NoNewPrivileges` / `ProtectSystem=strict` / `PrivateTmp` | yes | yes | — | — |
| `EnvironmentFile=/etc/robothor/robothor.env` | yes | yes | via drop-ins | yes |
| `EnvironmentFile=-/run/robothor/secrets.env` | yes | yes | yes | yes |
| `Requires=robothor-secrets.service` | yes | yes | yes | yes |

The orchestrator's `KillMode=mixed` is the one exception the unit explains for
itself: it sends `SIGTERM` to uvicorn directly rather than to the whole cgroup,
because under the default mode every stop timed out. Its `Restart=on-failure`
carries no rationale anywhere in the tree — the observable difference is simply
that a **clean** exit is not restarted there, where the other three restart
whatever the exit status. Its `restart-forever.conf` drop-in also sets
`StartLimitIntervalSec=0`, so a crash loop is never rate-limited into a
permanently stopped service.

Every unit has the secrets dependency, which is the one that must never be
optional — a bridge with no shared SSO secret is worse than a bridge that is
not running.

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
# EnvironmentFile=-<workspace>/genus.env
```

or copy the two lines into `/etc/robothor/robothor.env`. Without them the
bridge refuses every `/api/auth/sso` exchange and nobody can sign in. That used
to be invisible: the bridge's `/ready` answered green for eight days while one
instance was locked out of its own dashboard. Both halves now report it — the
bridge's `/ready` carries an `sso_secret` check, and `genus doctor`'s
`secrets.bridge_sso` is the one to run from a terminal.

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
secret_source("OPENROUTER_API_KEY")   # "env" | "vault" | "missing" | "unavailable"
```

It walks the process environment (whatever the backend put there) and then the
encrypted vault (`genus vault set …`), and never raises. The fourth source value
is the load-bearing one: **`unavailable` is not `missing`**. `missing` means the
credential is genuinely not configured; `unavailable` means the vault could not
be read, so nothing can say whether it holds the key. Collapsing the two is how
an unreachable credential store reads as an empty one. An unreadable vault sits
out five minutes before it is probed again, rather than putting a synchronous
database connect on the LLM hot path.

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
| Provider key | The instance vault, slot 1 | Secrets never go in config.yaml, which gets copied into bug reports. |
| Telegram bot token | `genus.env` on the compose substrate; **nowhere** on `local`, where `--telegram-token` verifies it and records only the bot's name | The channel and `genus doctor` both read `ROBOTHOR_TELEGRAM_BOT_TOKEN` from the environment — see the [quick start](quickstart.md#next-steps). |
| Resume state | `<workspace>/.robothor/init_state.yaml` | One `step: completed` line each. |

Re-running is routine. An existing `owner.yaml` is never overwritten, and
completed steps are skipped — except `ack`, `signin`, `services`, `verify` and
`link`, which run every time because each one mints, prints or checks something
that has to be current. On the compose substrate `render`, `up-infra`, `up` and
`wait` re-run too: reconciling a stack that is already up is what `up -d` is
for.

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

The printed address is `127.0.0.1` because that is the only address the wizard
can know is right: guessing a public hostname would hand the operator a URL
that does not resolve and a token that has already started expiring.
That is loopback on the **server**, so forward the port rather than exposing it:

```bash
ssh -L 3004:127.0.0.1:3004 box.example.test
```

Then open the printed link on your own machine. `genus init` prints this line
for you whenever stdin is not a terminal — a container, a provisioning script —
because then nobody is sitting at the machine the loopback address belongs to.

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

Every release publishes the packaged chart to GHCR, so an install needs no
clone:

```bash
helm install genus oci://ghcr.io/ironsail-llc/charts/genus-os \
  --version X.Y.Z \
  --namespace genus --create-namespace \
  --values values.yaml

helm test genus --namespace genus
```

The chart version is the release tag with the leading `v` removed — the same
`vX.Y.Z` the images carry, because semantic-release bumps `Chart.yaml` in the
commit the tag points at and the publish job refuses to push when the two
disagree. `helm show values oci://ghcr.io/ironsail-llc/charts/genus-os
--version X.Y.Z` prints the defaults to start your own `values.yaml` from, and
an upgrade is the same line with `helm upgrade`.

**From a checkout**, for chart development and for a change you have not
released yet:

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

`minimal` is three agents (one of them with a nightly schedule of its own),
`standard` adds email triage,
calendar watch and briefings, `full` is the whole catalogue. `genus init
--preset NAME` does it during an install.

## Production Checklist

- [ ] Set a strong `ROBOTHOR_DB_PASSWORD`
- [ ] PostgreSQL: tune `max_connections` (recommended: 200), `shared_buffers`, `work_mem`
- [ ] PostgreSQL: enable SSL for remote connections
- [ ] Redis: set a password if exposed beyond localhost
- [ ] Redis: set `maxmemory` and `appendonly yes` for durability
- [ ] Ollama: confirm the two required models are pulled — `ollama list` should show
      `qwen3-embedding:0.6b` and `Qwen3-Reranker-0.6B:F16`, which are what
      `genus init` installs and what memory search needs
- [ ] Ollama: confirm work is landing where you expect — `ollama ps` lists what is
      loaded and where it is running. `ollama run` on an embedding model generates
      nothing, so use the generation model for that check *if* you pulled it (it is
      an optional multi-gigabyte model, not one of the two above)
- [ ] Run `genus doctor` and clear every `required` failure (exit 0)
- [ ] Run `genus migrate --status` and confirm no drift and nothing pending
- [ ] Set up log rotation for `/var/log/robothor/`
- [ ] Rebuild vector indexes after initial data load: `REINDEX INDEX idx_facts_embedding_active`
- [ ] Set `ROBOTHOR_CAPABILITIES_MANIFEST` if the deployment keeps its
      `agent_capabilities.json` outside the workspace
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
- `db.rls_coverage` — executes migration `120_tenant_rls_cover_new_tables.sql`, which
  policies every `tenant_id` table that has no `tenant_isolation` policy yet.

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

### The Helm's Observe pages

Three more operator surfaces on the bridge, behind the same operator gate as
`/api/doctor` (owner or admin, platform tenant, never a service token).

**Memory.** `GET /api/memory/facts` pages what this tenant believes, newest
first, filtered by `q` (which goes through the same search path as
`/api/memory/search`), `entity`, and `active=true|false|all`. The filters
compose: `q` with `entity` still filters by entity. `cursor` is keyset
pagination over the browse order only — combined with `q` it is a `422`, because
a relevance ranking has no keyset.
`POST /api/memory/facts/{id}/forget/preview` answers what a forget would do —
which entities the fact is filed under, how many nightly episodes cite it, and
which always-in-context memory blocks quote its text — and writes nothing. For
a fact under twelve characters the block scan is skipped and
`references.blocks_scanned` is `false`: a substring that short matches
everything, and a warning that cries wolf is the one that gets ignored.
`POST /api/memory/facts/{id}/forget` takes a reason (3–500 characters) and
**bounds** the fact: `is_active=false`, `valid_to=now()`. Nothing is deleted,
and only the named row changes — supersession chains are not followed. A second
forget is a `409`; a fact in another tenant is a `404`. The audit row carries
the fact id and the reason, never the text.

**Audit.** `GET /api/controls/audit` reads the guardrail change log
(`feature_flag_audit`, written on every `PATCH /api/controls`), newest first.
`GET /api/audit/events.csv` exports the audit log itself — same filters as
`/api/audit/events`, up to 5,000 rows, as a `text/csv` attachment. Both admit
the `auditor` role as well as an operator; nothing else on `/api/controls`
does. Cells are sanitised, run through the platform's secret redactor, and
anything a spreadsheet would evaluate is prefixed with an apostrophe, so an
audit detail cannot become a formula or carry a credential off the box. A
failed export is a real `500` rather than an empty file.

`audit_log` has no tenant column, so **the export is appliance-wide**, not
caller-tenant-wide — the same rows `GET /api/audit/events` has always returned.
The `<tenant>` in the filename names who exported it, not what is inside.

**Logs.** `GET /api/logs/units` lists the units that may be followed — derived
from the unit files `scripts/install-units.sh` installs, so a unit added to
`infra/systemd` appears without a code change — and `GET /api/logs?unit=…`
serves the last `lines` (1–1000) of one, with optional `since` (`30m`, `1h`,
`7d`, or an ISO-8601 timestamp) and a `grep` substring applied in the bridge,
never by journalctl. Every message passes through the platform's secret
redactor before it leaves the process.

**Logs need journald**, and a container does not have it. Both routes answer
`{"available": false, "reason": "…"}` with a `200` when `journalctl` is absent
or unusable — the Helm renders that as "not available on this deployment"
rather than an error. On a systemd install the bridge runs as
`$ROBOTHOR_SERVICE_USER`, which must be able to read the journals it is asked
for: membership of `systemd-journal` is enough for the system journal, and the
route reports `available: false` rather than empty output if it is missing. In
Kubernetes and Docker Compose, use the container runtime's own log stream.

### Using the Observe pages

The Helm's **Observe** group carries three screens over those routes. Memory
and Logs are owner/admin; Audit also admits the read-only `auditor` role, and
the navigation reflects that — an auditor sees Audit and not the other two.
The gating in the Helm is a convenience; the bridge checks the caller's role on
every request regardless of what the browser shows.

**Observe › Memory** lists what the instance believes, newest first, with a
search box, an entity filter and an active/inactive/all switch. Browsing pages
with a cursor; searching does not — a relevance ranking has no place to resume
from, so the page offers a wider ranking instead of a next page, and never
reports "N of M" (a search filtered by entity returns a short page for reasons
that have nothing to do with how many facts match).

**Forget** shows its consequences first. The confirmation names how many
episodes cite the fact and which always-in-context memory blocks quote its
text, and it asks for a reason of 3–500 characters that goes into the audit
record. What it does **not** do is the part worth reading: it bounds one row
(`is_active=false`, `valid_to=now()`) and nothing else. Nothing is deleted, no
supersession chain is followed, and **a memory block that quotes the text still
quotes it** — an agent carrying that block keeps repeating the fact until the
block is rewritten. For a fact under twelve characters the page says the text
is too short to check against the blocks rather than claiming there are none.

**Observe › Audit** has two tabs: the audit log itself, and the guardrail
change log (every `PATCH /api/controls`, with the reason its operator typed).
**Export CSV** is a plain download link carrying the same filters the table is
showing, up to 5,000 rows. It is **appliance-wide** — `audit_log` has no tenant
column, so it holds every tenant's rows, and the tenant in the filename names
who exported it, not what is inside. The events tab has no cursor: "Load more"
re-reads a larger window up to the route's 500-row ceiling, and anything bigger
is an export.

**Observe › Logs needs journald.** In a container there is none, and the page
says so in the bridge's own words rather than showing an error — nothing is
broken. Where journald is present, pick a unit from the derived allowlist,
choose 50/200/1000 lines and a time window, and filter. Auto-refresh is off by
default and beats every ten seconds when switched on: each read is one
`journalctl` process on the box. The "window full" notice means the line limit
was reached, not that there is nothing else to find, and the filter is applied
to the **redacted** message — so the pane cannot be used to confirm a value it
will not show.

### Using the Plugins page

**Settings › Plugins** is not an Observe page and does not sit on the routes
above — it is the operator screen over `/api/plugins`, and it is named here
because it is the other place the Helm writes to the box. It lists one card per
installed distribution, records the lockfile on a box nobody has shelled into,
turns a plugin off, and reloads the engine once afterwards. A damaged lockfile
is reported as what it is: the engine ignores such a file entirely, so every
plugin that was turned off is loading again. See
[Disabling and the lockfile](PLUGINS.md#disabling-and-the-lockfile).

## Health Endpoints

| Service | Endpoint | Expected |
|---------|----------|----------|
| Engine | `GET /health` on :18800 | `{"status": "ok"}`; `/ready` is the one an orchestrator gates on, and it carries the fleet check |
| Orchestrator (RAG API) | `GET /health` on :9099 | `{"status": "ok"}` |
| Bridge | `GET /health` on :9100 | `{"status": "ok"}`; its `/ready` also checks the orchestrator and the SSO secret |
| Dashboard | `GET /api/health` on :3004 | 200; `/api/ready` is what the compose healthcheck polls |
| Vision | `GET /health` on :8600 | `{"status": "ok", "mode": "..."}` |

## Verified fleet artifacts

`robothor.templates.fleet_release.build_release(source, destination, specification)`
compiles an explicit inventory of native agents, workflows, Markdown knowledge,
plugin wheels and optional sales settings into a new artifact directory. It
validates native contracts and references, inspects wheels without importing them,
rejects credential literals in text members, and hashes every member. Source and
platform Git revisions are required metadata; the caller must establish their
provenance. Build into a trusted output directory with a unique destination.

The returned `release_id` fingerprints the complete canonical metadata and member
hashes. Retain it outside the artifact. Call
`verify_release(artifact, expected_digest=release_id)` to detect changed, missing,
additional or symlinked members. Reading the expected digest from the artifact
itself would not establish the externally reviewed version.

The artifact records `activation: not_installed`. Sales integration switches must
be disabled in its settings. Artifact publication does not install plugins, change
the workspace, reconcile schedules, migrate a database or enable integrations.
Coordinated runtime cutover, rollback, plugin installation checks and readiness
remain separate deployment requirements. Existing individual-agent installation
must not be described as an atomic fleet cutover.

`robothor.templates.fleet_snapshot.load_snapshot(artifact, expected_digest=...)`
captures verified bytes in memory and checks member hashes again during capture.
Its `agent(agent_id)` method returns a fresh native `AgentConfig` with immutable
knowledge tuples. `build_system_prompt` and declared warmup-context loading use
those captured files, bypassing workspace reads and the legacy prompt cache.
They refuse references outside the snapshot. Changing or removing the artifact
after admission cannot substitute knowledge in that admitted run; subsequent
admissions reverify it. The [sales runtime](SALES_INTELLIGENCE.md#native-workflow-execution)
supports selecting such a release explicitly. Other manifest-loading callers
retain their existing behavior until a coordinated installer integrates them.

`robothor.templates.fleet_store.stage_release(source, workspace,
expected_digest=...)` privately stages the complete verified artifact at
`.robothor/fleet-releases/<fingerprint>`. Concurrent staging is serialized;
repeated staging reverifies the existing destination. Drift is refused rather
than repaired in place. Files and directory entries are flushed before the
completed directory is published. Interrupted staging can be retried against the
same reviewed fingerprint. A crash after publication is recovered by reverifying
the destination. Hidden staging directories are never runtime lookup targets.

Staging does not change the active release setting, install wheel code, copy
manifests into the loose fleet directories or register schedules. Artifact
metadata remains unchanged. The native sales runner and stager share the same
validated lookup-path function.

Native sales queue ticks hold a shared `sales-fleet` maintenance gate for their
tenant. A controller can acquire an exclusive
`robothor.operations.gates.gate(operations, "sales-fleet")` before its settings
transaction; busy gates refuse immediately. Acquire this gate before settings
or work-row locks. Shared gates allow stop, inbox and research work to proceed
concurrently. Cancelling a caller waits for its bounded worker and gate cleanup,
so a workflow timeout does not falsely report an idle worker boundary.

The gate uses a pooled PostgreSQL transaction for the duration of a queue tick.
A future cutover controller must also inspect durable job/action leases and
unresolved provider effects: an available advisory gate alone cannot prove
quiescence after a process or database failure. Schedule generation, deployed
platform/plugin identity, rollback and activation acknowledgements remain
coordinator requirements.

### Durable sales deployment transitions

Migration 130 adds monotonic sales-settings revisions and tenant-scoped
`sales_deployments` records. `robothor.sales.deployment.DeploymentCoordinator`
prepares a transition against an exact settings revision and staged artifact.
It takes the exclusive maintenance gate, checks running sales jobs (including
expired leases), executing/unknown sales actions and unresolved sales/Pipedrive/
Instantly effects, then records the previous and proposed configurations. Only
one transition may prepare per tenant. The durable pending record blocks native
queue admission across controller/process restarts.

`commit` requires a runtime verifier dependency and rechecks the artifact,
settings revision and unfinished-work conditions. It writes the selected release,
agent/workflow bindings, transition status and audit event in one transaction.
All five integration switches are off after deployment. Current operational
settings, including budgets and policy references, are preserved. A settings edit
after preparation invalidates commit, even if later edits restore the same values.
The normal settings API cannot bypass the coordinator to change managed bindings.

`robothor.engine.source_identity.SourceIdentity` supplies a source-checkout
identity primitive. Capture it once when establishing a runtime generation and
retain it: verification requires the expected clean Git revision and unchanged
tracked-file inventory, including filesystem modification history. Untracked
files in the engine and bridge package trees also refuse verification. A source
edit followed by restoration still requires a fresh generation. The native sales
runtime captures this identity before subsystem construction. Wheel/container installations without a Git
checkout require separate build provenance; a version label is insufficient.
Installed plugin payload verification is described in [Plugins](PLUGINS.md#installed-payload-verification).
Neither primitive by itself establishes loaded-code or schedule readiness.

`prepare_rollback` creates another guarded transition from the latest committed
deployment to its previous structural configuration. It preserves current
operational settings and also leaves integration switches off. `abort` requires
verification that the previous runtime has been restored before it clears the
pending record; it preserves newer operator settings. Failed verification or a
transaction failure leaves the pending transition recoverable and admission closed.

`robothor.engine.sales_runtime.NativeSalesRuntime` supplies the native verifier
for source-checkout engines and service-only fleet plugins. Its async `prepare`,
`commit` and `abort` methods serialize control on the engine loop. Commit/abort
reconcile the selected snapshot, then run the database coordinator off-loop; the
coordinator's synchronous verification bridges back to the owning loop. Evidence
is bound to the exact durable transition, restoration direction and schedule
generation. Callers cannot submit proof dictionaries. Cancellation drains the
actual control transaction before releasing the control lock.

The daemon captures source and plugin startup identity before subsystem
construction, attaches the runtime to its native scheduler, and bootstraps it
after APScheduler starts. A committed selection is reverified and reconciled.
A pending transition loads no managed jobs and remains pending until explicit
commit or abort; restart never chooses between them. `/ready` includes a native
sales check. Failed sales bootstrap preserves core engine/operator access while
managed admission stays closed. Every managed queue admission also verifies
artifact bytes, source identity, installed plugin payload and schedule state;
file work runs off-loop and completes before a worker starts.

Plugin checks pin the boot governance record, installed file history and native
plugin generation. Replacement, edit/restoration or reload requires restart.
Service factories must resolve through the native registry from declared files
inside the governed installed distribution. Runtime inspection uses captured
wheel bytes. Other plugin extension groups and non-Git build provenance need
explicit support. Native preparation refuses an unsupported nonempty unmanaged
sales baseline before creating a transition. Production cutover remains separate
from these implemented controls and their isolated validation.

### Helm sales deployment controls

Open **Sales → Manage sales deployment**. A new workspace can be initialized with
all five integrations explicitly paused through the existing sales settings API.
Inspect a separately retained staged-release fingerprint, review its source and
platform revisions and agent/workflow/adapter inventory, and enter a change reason.
Preparation uses the displayed settings revision. It closes queue admission but
does not install or select the release. The pending panel shows the immutable
target and the restoration target before commit or cancellation.

The engine mounts human-only routes under `/api/admin/sales-deployment`:

| Method | Suffix | Behavior |
| --- | --- | --- |
| GET | root | Selected release, settings revision, pending transition, readiness, control activity and up to 20 recent transitions |
| GET | `/releases/{fingerprint}` | Verify and describe a staged artifact without selecting it or returning filesystem paths |
| POST | `/prepare` | Prepare the exact release using `release_id`, `expected_revision` and `reason` |
| POST | `/transitions/{id}/commit` | Reconcile and commit the exact pending transition; body is `{}` |
| POST | `/transitions/{id}/abort` | Verify restoration before cancellation; requires `reason` |
| POST | `/transitions/{id}/rollback` | Prepare rollback of the latest committed transition using `expected_revision` and `reason`; does not commit it |

Every route requires a verified human owner/admin identity, `engine:control` and
the engine's tenant. Service tokens and insecure development identities are
refused. Request contracts reject caller-supplied tenants, actors, readiness proof
and activation switches. Preparation attribution is shown as **Prepared by**;
the actual committing/restoring human is independently recorded in the operation
audit. Readiness responses report observed engine state, not client assertions.

The narrow Next.js `/api/sales/deployment/[[...path]]` proxy forwards the verified
human session token directly to the engine. It accepts only the listed operation
shapes and typed identifiers, ignores caller authorization headers, refuses
redirects and does not cache responses. It never substitutes a bridge service
identity. UI initialization uses the existing human-scoped bridge settings API.

The UI invalidates inspection when the fingerprint changes and disables mutations
while another control operation runs. An uncertain response never causes an
automatic retry: refresh authoritative status before another change. Successful
commit and rollback leave integrations paused. A staged artifact or an installed
fleet is not proof that provider connectivity or the customer pilot has succeeded.

### Managed workflow schedule generations

`robothor.engine.fleet_schedules.FleetSchedules` reconciles a verified snapshot's
cron workflows through the native workflow engine and APScheduler. It owns only
the definitions and jobs it introduces, refuses collisions with loose workflows,
removes retired jobs and preserves unrelated schedules. Call it on its owning
engine event loop while the deployment's durable pending record blocks queue
admission. Interrupted reconciliation invalidates the generation until retried.
The initial supported baseline is an empty managed fleet; existing loose sales
workflows require a separately verified migration rather than silent adoption.

Verification checks the exact workflow definitions, callback identity and
arguments, cron fields/timezone, running scheduler, non-paused jobs and execution
limits. Every reconciliation creates a fresh generation, even when restoring the
same release. An old callback or an in-flight workflow reaching the queue after
reconciliation is refused. A managed queue tick requires an in-process native
invocation matching its tenant, selected release and workflow; it rechecks the
generation inside shared admission before invoking a worker. Unmanaged fleets
retain their existing behavior. HTTP arguments cannot supply this context.

This component is tested through native scheduled execution, tool registry,
service identity and queue admission against the isolated database. Additional
native deployment tests exercise actual source verification, coordinator commit,
restart, rollback, restoration and cancellation recovery. These establish the
integrated deployment path in isolation; they do not prove a live customer pilot.

## Directory Structure (systemd install)

The unit templates spell the workspace `/opt/robothor` and
`scripts/install-units.sh` renders that to `$ROBOTHOR_WORKSPACE`, so the first
path below is wherever you pointed the workspace:

```
/opt/robothor/                        # the workspace ($ROBOTHOR_WORKSPACE): brain/, docs/agents/, .robothor/
/etc/robothor/robothor.env            # unit environment; the install step above
                                      # sets it 640, nothing enforces that
/run/robothor/secrets.env             # tmpfs, 0600, written by robothor-secrets.service
/run/robothor/restart-requests/       # 0700; the filename is the authorization
/var/log/robothor/                    # logs
/var/lib/robothor/backup-state/       # last-good backup markers — must survive a reboot
/var/lib/robothor/slo-state/          # the daily watch's last-good marker
/var/lib/robothor/alert-spool/        # pages that could not be delivered yet
/usr/local/lib/robothor/              # the restart handler, root-owned and outside the checkout
```

`/var/lib/robothor` holds evidence that has to outlive a reboot, which is why
those three are not on tmpfs — a marker that vanishes at boot reads as an
outage that is not happening.
