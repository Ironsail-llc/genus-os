# Deployment

Three deployment paths: Docker Compose (fastest), systemd services (production), or manual (custom).

## Docker Compose

The included `infra/docker-compose.yml` provides PostgreSQL (pgvector), Redis, and Ollama:

```bash
# Clone and configure
git clone https://github.com/Ironsail-llc/genus-os.git
cd genus-os
cp infra/robothor.env.example .env
# Edit .env -- set at minimum: ROBOTHOR_DB_PASSWORD

# Install the CLI (this is what runs the migrations)
pip install -e .

# Start infrastructure
docker compose -f infra/docker-compose.yml up -d

# Wait for PostgreSQL to pass its healthcheck before migrating
until [ "$(docker inspect -f '{{.State.Health.Status}}' robothor-postgres)" = healthy ]; do
  sleep 2
done

# Create the schema, then confirm it
robothor migrate
robothor migrate --status

# Verify the containers
docker compose -f infra/docker-compose.yml ps
```

The Compose file includes health checks for all services. It does **not** seed
the schema: `robothor migrate` is the only thing that creates or changes it, so
every applied file is recorded in the `schema_migrations_v2` ledger with its
SHA-256 checksum. A schema created any other way (an SQL file mounted into
`docker-entrypoint-initdb.d`, `psql -f`) leaves that ledger empty and later
upgrades cannot tell it apart from an empty database.

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

The Compose file reserves all NVIDIA GPUs by default. For CPU-only:

```yaml
# In docker-compose.yml, replace the deploy block:
ollama:
  deploy: {}
```

## Systemd Services

Template unit files are in `infra/systemd/`. Install them:

```bash
# Copy service files
sudo cp infra/systemd/robothor-*.service /etc/systemd/system/

# Copy environment config
sudo mkdir -p /etc/robothor
sudo cp infra/robothor.env.example /etc/robothor/robothor.env
sudo chmod 640 /etc/robothor/robothor.env
# Edit /etc/robothor/robothor.env with production values

# Create system user
sudo useradd -r -s /usr/sbin/nologin -d /opt/robothor robothor
sudo mkdir -p /opt/robothor /var/log/robothor
sudo chown robothor:robothor /opt/robothor /var/log/robothor

# Install the package
sudo -u robothor pip install "genusos[all]"

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now robothor-api
```

Service features:
- `Restart=always` with 5s backoff
- `KillMode=control-group` (no orphaned children)
- Security hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`
- `EnvironmentFile=/etc/robothor/robothor.env`

View logs: `journalctl -u robothor-api -f`

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

## Manual Setup

### PostgreSQL

```bash
# Install pgvector extension
sudo apt install postgresql-16-pgvector  # Debian/Ubuntu

# Create database
sudo -u postgres createdb robothor_memory
sudo -u postgres psql -d robothor_memory -c "CREATE EXTENSION vector"
sudo -u postgres psql -d robothor_memory -c "CREATE EXTENSION \"uuid-ossp\""

# Create application user
sudo -u postgres psql -c "CREATE USER robothor WITH PASSWORD 'your-password'"
sudo -u postgres psql -c "GRANT ALL ON DATABASE robothor_memory TO robothor"

# Run schema migrations (the whole manifest, recorded in schema_migrations_v2)
robothor migrate
robothor migrate --status

sudo -u postgres psql -d robothor_memory -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO robothor"
sudo -u postgres psql -d robothor_memory -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO robothor"
```

`robothor migrate` reads `ROBOTHOR_DB_HOST`, `ROBOTHOR_DB_PORT`,
`ROBOTHOR_DB_NAME`, `ROBOTHOR_DB_USER` and `ROBOTHOR_DB_PASSWORD`.

If you are upgrading a database whose schema this migrator did not create — one
seeded by an SQL file mounted into `docker-entrypoint-initdb.d`, or migrated by
the retired `robothor upgrade` glob — it refuses to run rather than replay
migrations over your data. Replaying is not harmless: `019_unified_session.sql`
deletes chat sessions, `035_drop_legacy_buddy_columns.sql` aborts mid-chain.

Adopt the history once. This records the baseline, plus every migration named in
the legacy `.robothor/migrations_applied.yaml` side-ledger, as applied *without
executing them*, then applies whatever genuinely remains:

```bash
robothor migrate --adopt-baseline
```

Keep `.robothor/migrations_applied.yaml` until this has run — its `migrations:`
list is the only record of what the old path applied. `robothor upgrade` retires
that list on its own, once `schema_migrations_v2` covers every entry it names.
`robothor migrate --status` shows the provenance of each row.

If that side-ledger is gone and the schema is past the baseline, `--adopt-baseline`
refuses a second time: adopting only `001_init` and then running everything after
it would be the same replay. Say how far the schema actually got instead:

```bash
robothor migrate --status  # spell the id exactly; do not trust the applied/pending
                           # split here — reconciled rows are hearsay
robothor migrate --adopt-through 040_memory_episodes
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
`genus user password <email>`.

## Production Checklist

- [ ] Set a strong `ROBOTHOR_DB_PASSWORD`
- [ ] PostgreSQL: tune `max_connections` (recommended: 200), `shared_buffers`, `work_mem`
- [ ] PostgreSQL: enable SSL for remote connections
- [ ] Redis: set a password if exposed beyond localhost
- [ ] Redis: set `maxmemory` and `appendonly yes` for durability
- [ ] Ollama: verify GPU access with `ollama run qwen3-embedding:0.6b`
- [ ] Run `robothor status` to verify connectivity
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
