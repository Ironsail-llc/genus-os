# Infrastructure

## Hardware

**Lenovo ThinkStation PGX (DGX Spark)**
- CPU: NVIDIA Grace (ARM Cortex-X925), 20 cores, aarch64
- GPU: NVIDIA Blackwell GB10 (integrated)
- Memory: 128 GB unified (CPU + GPU shared)
- OS: Ubuntu 24.04, Linux 6.14.0-1015-nvidia
- Location: 29 W 16th Road, Broad Channel, NY 11693

## External Storage

**SanDisk SSD** (1.8 TB, USB-attached)
- Encryption: LUKS2 (`/dev/sda1`)
- LUKS UUID: `51c84483-396d-4f88-848e-58d0a060d7ec`
- Keyfile: `/root/robothor-backup.key` (slot 0, auto-unlock via `/etc/crypttab`)
- Passphrase: Slot 1 fallback
- Mount: `/mnt/robothor-backup` (ext4, `nofail` — boots fine if unplugged)
- Filesystem label: `robothor-backup`
- Ownership: `philip:philip`
- Backup script: `scripts/backup-ssd.sh` (daily 4:30 AM)
- Contents: project dirs, config, 2x DB dumps (30-day retention), Docker volumes, credentials, manifest

## Networking

### Cloudflare Tunnel

Domain: `robothor.ai`
Tunnel ID: `***REDACTED_CF_TUNNEL***`
Service: `cloudflared.service` (system-level, auto-starts)
Config: `tunnel/config.yml`

| Hostname | Backend | Auth | Purpose |
|----------|---------|------|---------|
| cam.robothor.ai | localhost:8890 | Cloudflare Access (email OTP) | Webcam HLS live stream |
| robothor.ai | localhost:3000 | Public | Status server (homepage) |
| status.robothor.ai | localhost:3001 | Public | Status dashboard |
| dashboard.robothor.ai | localhost:3001 | Public | Status dashboard (alias) |
| privacy.robothor.ai | localhost:3002 | Public | Privacy policy page |
| ops.robothor.ai | localhost:3003 | Cloudflare Access (email OTP) | Ops dashboard |
| voice.robothor.ai | localhost:8765 | Public | Twilio voice server |
| sms.robothor.ai | localhost:8766 | Public | Twilio SMS webhook |
| gateway.robothor.ai | localhost:18789 | Cloudflare Access (email OTP) | OpenClaw gateway |
| bridge.robothor.ai | localhost:9100 | Cloudflare Access (email OTP) | Bridge service API |
| orchestrator.robothor.ai | localhost:9099 | Cloudflare Access (email OTP) | RAG orchestrator API |
| vision.robothor.ai | localhost:8600 | Cloudflare Access (email OTP) | Vision API |
| monitor.robothor.ai | localhost:3010 | Cloudflare Access (email OTP) | Uptime Kuma monitoring |
| vault.robothor.ai | localhost:8222 | Cloudflare Access (email OTP) | Vaultwarden password vault |
| * (catch-all) | http_status:404 | — | — |

**Cloudflare Access (Zero Trust):** 8 apps protected with email OTP — only `philip@ironsail.ai` and `robothor@ironsail.ai` can access. 24h sessions. Protected apps: cam, gateway, ops, bridge, orchestrator, vision, monitor, vault.

API tokens documented in `brain/TOOLS.md` (tunnel-edit, DNS-edit, Access-edit).

### Tailscale

| Field | Value |
|-------|-------|
| IP | 100.91.221.100 |
| Hostname | thinkstationpgx-9c59 |
| Tailnet | ironsail |
| Service | tailscaled.service (system-level) |

## Database

**PostgreSQL 16 + pgvector 0.6.0**

Config: `listen_addresses = 'localhost,172.17.0.1'`, `max_connections = 200`
Docker access: `pg_hba.conf` allows TCP from `172.16.0.0/12` with `scram-sha-256`
PG password stored in SOPS-encrypted secrets (not plaintext).

### robothor_memory (primary)

User: `philip` (local peer auth) + `postgres` (legacy tables)

| Table | Owner | Purpose |
|-------|-------|---------|
| long_term_memory | postgres | Permanent memories with embeddings |
| short_term_memory | postgres | 48h TTL working memories |
| memory_facts | philip | Structured facts (categorized, confidence-scored) |
| memory_entities | philip | Knowledge graph nodes (people, projects, tech) |
| memory_relations | philip | Knowledge graph edges |
| contact_identifiers | philip | Cross-system contact resolution (channel → person_id + entity) |
| crm_people | philip | CRM contacts (replaces Twenty CRM) |
| crm_companies | philip | CRM companies |
| crm_notes | philip | CRM notes |
| crm_tasks | philip | CRM tasks |
| crm_conversations | philip | CRM conversations |
| crm_messages | philip | CRM messages |
| agent_memory_blocks | philip | Structured working memory blocks (persona, user_profile, working_context, etc.) |
| audit_log | postgres | System audit trail |

Embeddings: 1024-dim vectors via Qwen3-Embedding, indexed with pgvector ivfflat.

### vaultwarden

Used by Vaultwarden Docker container. Self-hosted password vault.
Web UI: `vault.robothor.ai` (Cloudflare Access protected)

## Redis

**Redis 7** on `127.0.0.1:6379` + `172.17.0.1:6379` (Docker bridge)

Config: `maxmemory 2gb`, `protected-mode no` (localhost + Docker only)
Shared by: RAG orchestrator cache
Service: `redis-server.service` (system-level, auto-starts)

## Ollama (localhost:11434)

| Model | Params | Quant | Size | Role |
|-------|--------|-------|------|------|
| llama3.2-vision:11b | 11B | — | 7.8 GB | Vision analysis, intelligence pipeline |
| qwen3-embedding:0.6b | 595M | Q8_0 | 639 MB | Dense embeddings (always loaded) |
| Qwen3-Reranker-0.6B:F16 | ~600M | F16 | 1.2 GB | Cross-encoder reranking (always loaded) |

**Loaded on demand (not always resident):**
- qwen3-next:latest (79.7B, Q4_K_M, ~50 GB VRAM) — RAG generation only

Ollama version: v0.15.5-rc2

## Vision Stack

| Component | Detail |
|-----------|--------|
| Camera | USB webcam at /dev/video0 |
| RTSP server | MediaMTX at rtsp://localhost:8554/webcam |
| HLS stream | http://localhost:8890/webcam/ → cam.robothor.ai |
| Resolution | 640x480 @ 30fps H.264 |
| Object detection | YOLOv8-nano (~6 MB) |
| Face recognition | InsightFace buffalo_l ArcFace (~300 MB) |
| VLM | llama3.2-vision:11b via Ollama (on-demand) |
| Face match threshold | cosine similarity > 0.45 |
| Snapshots | brain/memory/snapshots/ (30-day retention) |
| Face data | brain/memory/faces/enrolled_faces.json |
| Service | robothor-vision.service (system-level) |
| Health | http://localhost:8600/health |
| Modes | disarmed (idle) → basic (smart detection + Telegram alerts) → armed (same + tracking) |
| Remote access | https://cam.robothor.ai/webcam/ (Cloudflare Access) |

All camera ports bound to `127.0.0.1` — no direct network access. External access only through Cloudflare tunnel with Zero Trust auth.

Deep reference: `brain/VISION.md`

## Secrets Management

**SOPS + age** — encrypted credential storage, decrypted at runtime to tmpfs.

| Component | Path | Permissions |
|-----------|------|-------------|
| Age keypair | `/etc/robothor/age.key` | root:philip 640 |
| Encrypted secrets | `/etc/robothor/secrets.enc.json` | root:philip 640 |
| Runtime decrypted | `/run/robothor/secrets.env` | philip:philip 600 (tmpfs) |
| SOPS config | `/etc/robothor/.sops.yaml` | root 644 |

**Age public key:** `age186mguvnypf7mun49dhn83cm59dva4vvdv3lp2sjch4jj4vdhhalq6uwgt3`

**Credentials stored (~30 keys):** GOG keyring, Telegram bot token + chat ID, PostgreSQL password, GitHub token, Jira token, Cloudflare account email + tunnel/DNS/Access tokens + account/tunnel IDs, ElevenLabs key, N8N keys (API, REST JWT, MCP JWT), OpenAI/OpenRouter/Anthropic/Gemini API keys, gateway token, Vaultwarden admin token, Samba password.

**How it works:**
- `scripts/decrypt-secrets.sh` decrypts JSON → KEY=VALUE env file at `/run/robothor/secrets.env`
- Systemd services: `ExecStartPre=decrypt-secrets.sh` + `EnvironmentFile=/run/robothor/secrets.env`
- Cron jobs: wrapped with `scripts/cron-wrapper.sh` (sources secrets.env)
- `/run/robothor/` is tmpfs — secrets never persist to disk unencrypted
- Age private key is backed up to encrypted SSD daily

**Managing secrets:**
```bash
# Decrypt and edit (opens $EDITOR):
sudo SOPS_AGE_KEY_FILE=/etc/robothor/age.key sops /etc/robothor/secrets.enc.json

# Decrypt to stdout:
sudo SOPS_AGE_KEY_FILE=/etc/robothor/age.key sops -d /etc/robothor/secrets.enc.json

# After editing, restart affected services
sudo systemctl restart robothor-vision robothor-bridge
```

## Network Shares (Samba)

**Samba 4.19.5** — file sharing for LAN and Tailscale devices.

| Share | Path | Access |
|-------|------|--------|
| `robothor-backup` | `/mnt/robothor-backup` | Read-only, philip only |
| `robothor-projects` | `/home/philip` | Read-write, philip only |

**Access:** Local network (`192.168.1.0/24`) and Tailscale (`100.64.0.0/10`) only. Not exposed to internet. UFW rules restrict port 445.

**Services:** `smbd.service`, `nmbd.service` (system-level, enabled on boot)

**Connect:** `smb://100.91.221.100/robothor-backup` (Tailscale) or `smb://192.168.1.x/robothor-backup` (LAN)

**Credentials:** Samba user `philip`, password in SOPS (`SAMBA_PASSWORD`).

## GCP

| Field | Value |
|-------|-------|
| Project | robothor-485903 (Project #152250299895) |
| Service account | moltbot-chat@robothor-485903.iam.gserviceaccount.com |
| Key file | ~/.moltbot/googlechat-service-account.json |
| Services | Google Chat, NotebookLM Enterprise |

## RAG Orchestrator (port 9099)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| /health | GET | Component status |
| /query | POST | RAG query (question + profile) |
| /v1/chat/completions | POST | OpenAI-compatible chat |
| /profiles | GET | List RAG profiles |
| /stats | GET | Memory statistics |
| /ingest | POST | Cross-channel ingestion |
| /vision/* | GET/POST | Vision service proxy endpoints |

**RAG profiles:** fast, general, research, expert, heavy, code

Start: `brain/memory_system/start_rag.sh`
Now managed by: `robothor-orchestrator.service` (auto-starts)

## CRM Stack

**Native PostgreSQL tables + Bridge** — Unified contact/conversation management. CRM data lives in `crm_*` tables in `robothor_memory` (replaced former Twenty CRM + Chatwoot Docker containers, both removed).

### Docker Containers (managed by `robothor-crm.service`)

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| vaultwarden | vaultwarden/server:latest | 8222 | Password vault (Vaultwarden) |
| uptime-kuma | louislam/uptime-kuma:1 | 3010 | Service monitoring (Uptime Kuma) |
| kokoro-tts | ghcr.io/remsky/kokoro-fastapi | 8880 | Local TTS (Kokoro) |

Docker Compose: `crm/docker-compose.yml`
Secrets: `crm/.env` (Docker reads directly) + SOPS-encrypted `/etc/robothor/secrets.enc.json` (services)

### Bridge Service (port 9100)

FastAPI app connecting CRM tables and Memory System. Queries native PostgreSQL `crm_*` tables via `crm_dal`.
Managed by: `robothor-bridge.service`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| /health | GET | Connectivity check to all dependent services |
| /resolve-contact | POST | Channel + identifier → person_id + entity ID |
| /timeline/{identifier} | GET | Unified timeline across all systems |
| /webhooks/openclaw | POST | OpenClaw messages → CRM + contact resolution |
| /log-interaction | POST | Agent interaction logging → CRM |
| /api/conversations | GET | List conversations (query: status, page) |
| /api/conversations/{id} | GET | Get single conversation |
| /api/conversations/{id}/messages | GET | List messages in a conversation |
| /api/conversations/{id}/messages | POST | Create message in a conversation |
| /api/people | GET | List/search CRM people (query: search, limit) |
| /api/people | POST | Create person in CRM |
| /api/notes | POST | Create note in CRM |

The `/api/*` endpoints are REST proxies used by the OpenClaw `crm-tools` plugin. They query native PostgreSQL CRM tables via `crm_dal` so the agent doesn't need direct database credentials.

### MCP Servers (configured in `.claude.json`) — Claude Code only

| Server | Transport | Purpose |
|--------|-----------|---------|
| robothor-memory | stdio (Python) | Memory facts, entities, memory blocks, vision, interaction logging, CRM CRUD (people, companies, tasks, notes, conversations, messages, search) — all 28 tools |

### OpenClaw CRM Plugin — OpenClaw agent sessions only

**Plugin:** `crm-tools` at `clawd/.openclaw/extensions/crm-tools/index.ts`
**Config:** `openclaw.json` → `plugins.entries.crm-tools.enabled: true`

OpenClaw agent sessions (cron jobs, Telegram, Google Chat) can't use MCP servers (stdio-only). Instead, the `crm-tools` plugin provides the same tools via HTTP calls to the Bridge proxy endpoints above.

| Plugin Tool | Bridge Endpoint | Equivalent MCP Tool |
|-------------|----------------|---------------------|
| `list_conversations` | GET /api/conversations | mcp__robothor-memory__list_conversations |
| `get_conversation` | GET /api/conversations/{id} | mcp__robothor-memory__get_conversation |
| `list_messages` | GET /api/conversations/{id}/messages | mcp__robothor-memory__list_messages |
| `create_message` | POST /api/conversations/{id}/messages | mcp__robothor-memory__create_message |
| `log_interaction` | POST /log-interaction | mcp__robothor-memory__log_interaction |
| `create_person` | POST /api/people | mcp__robothor-memory__create_person |
| `list_people` | GET /api/people | mcp__robothor-memory__list_people |
| `create_note` | POST /api/notes | mcp__robothor-memory__create_note |
| `crm_health` | GET /health | — |

Tool names are intentionally identical between MCP and plugin so agent instructions (WORKER.md, HEARTBEAT.md, jobs.json) work unchanged regardless of runtime.
