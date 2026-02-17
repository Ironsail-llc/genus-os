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
- Contents: project dirs, config, 3x DB dumps (30-day retention), Docker volumes, credentials, manifest

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
| gateway.robothor.ai | localhost:18789 | Cloudflare Access (email OTP) | OpenClaw gateway |
| crm.robothor.ai | localhost:3030 | Cloudflare Access (email OTP) | Twenty CRM web UI |
| inbox.robothor.ai | localhost:3100 | Cloudflare Access (email OTP) | Chatwoot conversation inbox |
| bridge.robothor.ai | localhost:9100 | Cloudflare Access (email OTP) | Bridge service API |
| orchestrator.robothor.ai | localhost:9099 | Cloudflare Access (email OTP) | RAG orchestrator API |
| vision.robothor.ai | localhost:8600 | Cloudflare Access (email OTP) | Vision API |
| * (catch-all) | http_status:404 | — | — |

**Cloudflare Access (Zero Trust):** 8 apps protected with email OTP — only `philip@ironsail.ai` and `robothor@ironsail.ai` can access. 24h sessions. Protected apps: cam, gateway, ops, crm, inbox, bridge, orchestrator, vision.

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

### robothor_memory (primary)

User: `philip` (local peer auth) + `postgres` (legacy tables)

| Table | Owner | Purpose |
|-------|-------|---------|
| long_term_memory | postgres | Permanent memories with embeddings |
| short_term_memory | postgres | 48h TTL working memories |
| memory_facts | philip | Structured facts (categorized, confidence-scored) |
| memory_entities | philip | Knowledge graph nodes (people, projects, tech) |
| memory_relations | philip | Knowledge graph edges |
| contact_identifiers | philip | Cross-system contact resolution (channel → Twenty + Chatwoot + entity) |
| agent_memory_blocks | philip | Structured working memory blocks (persona, user_profile, working_context, etc.) |
| audit_log | postgres | System audit trail |

Embeddings: 1024-dim vectors via Qwen3-Embedding, indexed with pgvector ivfflat.

### twenty_crm

Used by Twenty CRM Docker containers. Contact/company/relationship store.
Web UI: `crm.robothor.ai` (Cloudflare Access protected)

### chatwoot

Used by Chatwoot Docker containers. Unified conversation inbox.
Web UI: `inbox.robothor.ai` (Cloudflare Access protected)

## Redis

**Redis 7** on `127.0.0.1:6379` + `172.17.0.1:6379` (Docker bridge)

Config: `maxmemory 2gb`, `protected-mode no` (localhost + Docker only)
Shared by: Twenty CRM, Chatwoot, RAG orchestrator cache
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

**Twenty CRM + Chatwoot + Bridge** — Unified contact/conversation management.

### Docker Containers (managed by `robothor-crm.service`)

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| twenty-server | twentycrm/twenty:v0.43.0 | 3030 | Twenty CRM web app + REST/GraphQL API |
| twenty-worker | twentycrm/twenty:v0.43.0 | — | Twenty background jobs |
| chatwoot-rails | chatwoot/chatwoot:v3.16.0-ce | 3100 | Chatwoot web app + REST API |
| chatwoot-sidekiq | chatwoot/chatwoot:v3.16.0-ce | — | Chatwoot background jobs (Sidekiq) |

Docker Compose: `crm/docker-compose.yml`
Secrets: `crm/.env` (not committed)

### Bridge Service (port 9100)

FastAPI app connecting Twenty, Chatwoot, and Memory System.
Managed by: `robothor-bridge.service`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| /health | GET | Connectivity check to all dependent services |
| /resolve-contact | POST | Channel + identifier → Twenty ID + Chatwoot ID + entity ID |
| /timeline/{identifier} | GET | Unified timeline across all systems |
| /webhooks/chatwoot | POST | Chatwoot message events → memory ingestion |
| /webhooks/twenty | POST | Twenty CRM events (future) |
| /webhooks/openclaw | POST | OpenClaw messages → Chatwoot + contact resolution |
| /log-interaction | POST | Agent interaction logging → CRM |
| /api/conversations | GET | List Chatwoot conversations (query: status, page) |
| /api/conversations/{id} | GET | Get single Chatwoot conversation |
| /api/conversations/{id}/messages | GET | List messages in a conversation |
| /api/conversations/{id}/messages | POST | Create message in a conversation |
| /api/people | GET | List/search Twenty CRM people (query: search, limit) |
| /api/people | POST | Create person in Twenty CRM |
| /api/notes | POST | Create note in Twenty CRM |

The `/api/*` endpoints are REST proxies used by the OpenClaw `crm-tools` plugin. They wrap Chatwoot REST and Twenty GraphQL APIs so the agent doesn't need direct API credentials.

### MCP Servers (configured in `.claude.json`) — Claude Code only

| Server | Transport | Purpose |
|--------|-----------|---------|
| robothor-memory | stdio (Python) | Memory facts, entities, memory blocks, vision, interaction logging |
| twenty-crm | stdio (Node.js) | Twenty CRM CRUD: people, companies, tasks, notes, search |
| chatwoot | stdio (Node.js) | Chatwoot conversations, messages |

### OpenClaw CRM Plugin — OpenClaw agent sessions only

**Plugin:** `crm-tools` at `clawd/.openclaw/extensions/crm-tools/index.ts`
**Config:** `openclaw.json` → `plugins.entries.crm-tools.enabled: true`

OpenClaw agent sessions (cron jobs, Telegram, Google Chat) can't use MCP servers (stdio-only). Instead, the `crm-tools` plugin provides the same tools via HTTP calls to the Bridge proxy endpoints above.

| Plugin Tool | Bridge Endpoint | Equivalent MCP Tool |
|-------------|----------------|---------------------|
| `chatwoot_list_conversations` | GET /api/conversations | mcp__chatwoot__chatwoot_list_conversations |
| `chatwoot_get_conversation` | GET /api/conversations/{id} | mcp__chatwoot__chatwoot_get_conversation |
| `chatwoot_list_messages` | GET /api/conversations/{id}/messages | mcp__chatwoot__chatwoot_list_messages |
| `chatwoot_create_message` | POST /api/conversations/{id}/messages | mcp__chatwoot__chatwoot_create_message |
| `log_interaction` | POST /log-interaction | mcp__robothor-memory__log_interaction |
| `create_person` | POST /api/people | mcp__twenty-crm__create_person |
| `list_people` | GET /api/people | mcp__twenty-crm__list_people |
| `create_note` | POST /api/notes | mcp__twenty-crm__create_note |
| `crm_health` | GET /health | — |

Tool names are intentionally identical between MCP and plugin so agent instructions (WORKER.md, HEARTBEAT.md, jobs.json) work unchanged regardless of runtime.
