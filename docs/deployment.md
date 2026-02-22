# Deployment

Three deployment paths: Docker Compose (fastest), systemd services (production), or manual (custom).

## Docker Compose

The included `infra/docker-compose.yml` provides PostgreSQL (pgvector), Redis, and Ollama:

```bash
# Clone and configure
git clone https://github.com/robothor-ai/robothor.git
cd robothor
cp infra/robothor.env.example .env
# Edit .env -- set at minimum: ROBOTHOR_DB_PASSWORD

# Start infrastructure
docker compose -f infra/docker-compose.yml up -d

# Verify
docker compose -f infra/docker-compose.yml ps
```

The Compose file includes health checks for all services. PostgreSQL auto-runs migrations from `infra/migrations/` on first start.

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
sudo -u robothor pip install robothor[all]

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

# Run schema migration
sudo -u postgres psql -d robothor_memory -f infra/migrations/001_init.sql

# Create application user
sudo -u postgres psql -c "CREATE USER robothor WITH PASSWORD 'your-password'"
sudo -u postgres psql -c "GRANT ALL ON DATABASE robothor_memory TO robothor"
sudo -u postgres psql -d robothor_memory -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO robothor"
sudo -u postgres psql -d robothor_memory -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO robothor"
```

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
