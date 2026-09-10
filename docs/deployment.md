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
robothor migrate --status                        # find the last migration you hold
robothor migrate --adopt-through 040_memory_episodes
```

`--adopt-through <migration_id>` adopts every migration up to and including that
id without executing any of them, then applies the rest normally. It implies
`--adopt-baseline`, so it can be used on its own. Pick the id by checking which
tables and columns the database already has — erring *later* than the truth skips
migrations that never ran, so when unsure, name the earliest id you are certain of
and let the rest apply.

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
