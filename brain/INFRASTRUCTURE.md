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
| voice.robothor.ai | localhost:8765 | Public | Twilio voice: inbound + outbound calling (Gemini Live) |
| sms.robothor.ai | localhost:8766 | Public | Twilio SMS webhook |
| engine.robothor.ai | localhost:18800 | Cloudflare Access (email OTP) | Python Agent Engine |
| bridge.robothor.ai | localhost:9100 | Cloudflare Access (email OTP) | Bridge service API |
| orchestrator.robothor.ai | localhost:9099 | Cloudflare Access (email OTP) | RAG orchestrator API |
| vision.robothor.ai | localhost:8600 | Cloudflare Access (email OTP) | Vision API |
| monitor.robothor.ai | localhost:3010 | Cloudflare Access (email OTP) | Uptime Kuma monitoring |
| * (catch-all) | http_status:404 | — | — |

**Cloudflare Access (Zero Trust):** 7 apps protected with email OTP — only `philip@ironsail.ai` and `robothor@ironsail.ai` can access. 24h sessions. Protected apps: cam, ops, bridge, orchestrator, vision, monitor, engine.

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
| crm_tasks | philip | CRM tasks (TODO/IN_PROGRESS/REVIEW/DONE state machine) |
| crm_task_history | philip | Task transition audit trail (append-only) |
| crm_routines | philip | Recurring task templates (cron-scheduled) |
| crm_conversations | philip | CRM conversations |
| crm_messages | philip | CRM messages |
| crm_tenants | philip | Multi-tenancy: tenant hierarchy (id slug PK, display_name, parent_tenant_id, settings JSONB) |
| crm_agent_notifications | philip | Agent-to-agent notifications (typed, durable, with read/ack tracking) |
| agent_memory_blocks | philip | Structured working memory blocks (persona, user_profile, working_context, etc.) |
| health_heart_rate | philip | Continuous heart rate readings (BIGINT PK = Unix seconds) |
| health_stress | philip | Continuous stress levels (BIGINT PK) |
| health_body_battery | philip | Body battery readings with charged/drained (BIGINT PK) |
| health_sleep | philip | Nightly sleep data with stage breakdown (DATE PK) |
| health_steps | philip | Daily step totals with goals (DATE PK) |
| health_hrv | philip | Heart rate variability readings (BIGINT PK) |
| health_spo2 | philip | SpO2/pulse ox readings (BIGINT PK) |
| health_respiration | philip | Respiration rate readings (BIGINT PK) |
| health_resting_heart_rate | philip | Daily resting HR (DATE PK) |
| health_daily_summary | philip | Daily activity/calorie summary (DATE PK) |
| health_training_status | philip | Training load, VO2max, recovery (DATE PK) |
| health_activities | philip | GPS activities — runs, rides, etc. (BIGINT PK = activity_id) |
| health_sync_log | philip | Garmin sync audit trail (SERIAL PK) |
| vault_secrets | philip | Encrypted credential store (AES-256-GCM, agent-accessible) |
| audit_log | postgres | System audit trail |

Embeddings: 1024-dim vectors via Qwen3-Embedding, indexed with pgvector ivfflat.

## Redis

**Redis 7** on `127.0.0.1:6379` + `172.17.0.1:6379` (Docker bridge)

Config: `maxmemory 2gb`, `protected-mode no` (localhost + Docker only)
Shared by: RAG orchestrator cache, event bus (7 Redis Streams: agent, email, calendar, crm, vision, system, helm)
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

## Voice & Telephony (port 8765)

| Component | Detail |
|-----------|--------|
| Service | robothor-voice.service (system-level) |
| Phone | +1 (413) 408-6025 (Twilio) |
| Twilio Account | ***REDACTED_TWILIO_SID*** |
| Inbound | Twilio ConversationRelay → WebSocket → Gemini Live |
| Outbound | `POST /call` → Twilio REST API → TwiML webhook → Gemini Live |
| AI Model | Gemini 2.5 Flash Native Audio (Vertex AI, real-time audio-to-audio) |
| Voice | Charon (deep male) |
| Health | http://localhost:8765/health |
| External | voice.robothor.ai (Public) |
| Engine tool | `make_call(to, recipient, purpose)` |
| Credentials | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` in SOPS |

**Endpoints:**
- `POST /call` — initiate outbound call (JSON: `to`, `recipient`, `purpose`)
- `POST /twiml` — Twilio TwiML webhook (returns `<Connect><Stream>`)
- `GET /media-stream` — WebSocket for Twilio Media Streams ↔ Gemini Live bridge
- `POST /call-status` — Twilio call status callbacks

Deep reference: `brain/TOOLS.md` (Voice & Calling section)

## Secrets Management

**SOPS + age** — encrypted credential storage, decrypted at runtime to tmpfs.

| Component | Path | Permissions |
|-----------|------|-------------|
| Age keypair | `/etc/robothor/age.key` | root:philip 640 |
| Encrypted secrets | `/etc/robothor/secrets.enc.json` | root:philip 640 |
| Runtime decrypted | `/run/robothor/secrets.env` | philip:philip 600 (tmpfs) |
| SOPS config | `/etc/robothor/.sops.yaml` | root 644 |

**Age public key:** `age186mguvnypf7mun49dhn83cm59dva4vvdv3lp2sjch4jj4vdhhalq6uwgt3`

**Credentials stored (~28 keys):** GOG keyring, Telegram bot token + chat ID, PostgreSQL password, GitHub token, Jira token, Cloudflare account email + tunnel/DNS/Access tokens + account/tunnel IDs, ElevenLabs key, N8N keys (API, REST JWT, MCP JWT), OpenAI/OpenRouter/Anthropic/Gemini API keys, gateway token, Samba password.

### PostgreSQL Vault (agent-accessible credential store)

| Component | Path | Permissions |
|-----------|------|-------------|
| Master key | `.vault-key` (project root) | philip:philip 600, gitignored |
| Table | `vault_secrets` in `robothor_memory` | AES-256-GCM encrypted values |
| Package | `robothor/vault/` | `get`, `set`, `delete`, `list`, `export_env` |

Agents access credentials via `vault_get(key)` and `vault_set(key, value)` tools. SOPS is for infra env vars decrypted at boot; the vault is for runtime credential storage.

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
| /api/people | GET | List/search CRM people (query: search, limit) |
| /api/people | POST | Create person in CRM |
| /api/people/{id} | GET | Get person by ID |
| /api/people/{id} | PATCH | Update person fields |
| /api/people/{id} | DELETE | Soft-delete person |
| /api/people/merge | POST | Merge duplicate people (keeper absorbs loser) |
| /api/companies | GET | List/search companies |
| /api/companies | POST | Create company |
| /api/companies/{id} | PATCH | Update company fields |
| /api/companies/{id} | DELETE | Soft-delete company |
| /api/companies/merge | POST | Merge duplicate companies |
| /api/notes | GET | List notes (query: personId, companyId) |
| /api/notes | POST | Create note in CRM |
| /api/notes/{id} | PATCH | Update note |
| /api/notes/{id} | DELETE | Soft-delete note |
| /api/tasks | GET | List tasks (query: status, assignedToAgent, priority, tags) |
| /api/tasks | POST | Create task with agent coordination fields |
| /api/tasks/{id} | GET | Get task by ID |
| /api/tasks/{id} | PATCH | Update task fields (status, priority, tags, etc.) |
| /api/tasks/{id} | DELETE | Soft-delete task |
| /api/tasks/{id}/resolve | POST | Mark task DONE with resolution summary |
| /api/tasks/{id}/approve | POST | Approve REVIEW task (reviewer != assignee validation) |
| /api/tasks/{id}/reject | POST | Reject REVIEW task (reverts to IN_PROGRESS) |
| /api/tasks/{id}/history | GET | Task transition audit trail |
| /api/tasks/agent/{agent_id} | GET | Agent inbox (priority-ordered) |
| /api/conversations | GET | List conversations (query: status, page) |
| /api/conversations/{id} | GET | Get single conversation |
| /api/conversations/{id}/messages | GET | List messages in a conversation |
| /api/conversations/{id}/messages | POST | Create message in a conversation |
| /api/conversations/{id}/status | POST | Toggle conversation status |
| /api/routines | GET | List routines |
| /api/routines | POST | Create routine |
| /api/routines/{id} | GET | Get routine by ID |
| /api/routines/{id} | PATCH | Update routine |
| /api/routines/{id} | DELETE | Delete routine |
| /api/agents/status | GET | List agent statuses |
| /api/agents/{id}/status | GET | Get agent status by ID |
| /api/agents/{id}/status | POST | Update agent status |
| /api/notifications/send | POST | Send agent-to-agent notification |
| /api/notifications/inbox/{agent_id} | GET | Get agent notification inbox |
| /api/notifications/{id}/read | POST | Mark notification as read |
| /api/notifications/{id}/ack | POST | Acknowledge notification |
| /api/notifications | GET | List notifications (query: fromAgent, toAgent) |
| /api/tenants | GET | List tenants |
| /api/tenants | POST | Create tenant |
| /api/tenants/{id} | GET | Get tenant details |
| /api/tenants/{id} | PATCH | Update tenant |
| /api/memory/blocks | GET | List memory blocks |
| /api/memory/blocks/{name} | GET | Read memory block |
| /api/memory/blocks/{name} | PUT | Write memory block |
| /api/memory/blocks/{name}/append | POST | Append timestamped entry to block |
| /api/search | GET | Search across CRM tables |
| /api/metadata/objects | GET | List CRM object types |
| /api/metadata/objects/{name} | GET | Get column definitions |
| /api/audit | GET | Query audit log |
| /api/telemetry | GET | Query telemetry |
| /api/events | GET | SSE event stream |
| /api/impetus/* | * | Proxy to Impetus One platform |

All `/api/*` endpoints support `X-Tenant-Id` header for multi-tenant scoping (defaults to `robothor-primary`). The endpoints are REST proxies used by the OpenClaw `crm-tools` plugin. They query native PostgreSQL CRM tables via `crm_dal` so the agent doesn't need direct database credentials. Middleware stack: CorrelationMiddleware → TenantMiddleware → RBACMiddleware.

### MCP Servers (configured in `.claude.json`) — Claude Code only

| Server | Transport | Purpose |
|--------|-----------|---------|
| robothor-memory | stdio (Python) | Memory facts, entities, memory blocks, vision, interaction logging, CRM CRUD (people, companies, tasks, notes, conversations, messages, search) — all 28 tools |

### OpenClaw CRM Plugin — OpenClaw agent sessions only

**Plugin:** `crm-tools` at `brain/.openclaw/extensions/crm-tools/index.ts`
**Config:** `openclaw.json` → `plugins.entries.crm-tools.enabled: true`

OpenClaw agent sessions (cron jobs, Telegram, Google Chat) can't use MCP servers (stdio-only). Instead, the `crm-tools` plugin provides the same tools via HTTP calls to the Bridge proxy endpoints above.

| Plugin Tool | Bridge Endpoint | Equivalent MCP Tool |
|-------------|----------------|---------------------|
| `list_conversations` | GET /api/conversations | mcp__robothor-memory__list_conversations |
| `get_conversation` | GET /api/conversations/{id} | mcp__robothor-memory__get_conversation |
| `list_messages` | GET /api/conversations/{id}/messages | mcp__robothor-memory__list_messages |
| `create_message` | POST /api/conversations/{id}/messages | mcp__robothor-memory__create_message |
| `toggle_conversation_status` | POST /api/conversations/{id}/status | mcp__robothor-memory__toggle_conversation_status |
| `log_interaction` | POST /log-interaction | mcp__robothor-memory__log_interaction |
| `create_person` | POST /api/people | mcp__robothor-memory__create_person |
| `update_person` | PATCH /api/people/{id} | mcp__robothor-memory__update_person |
| `list_people` | GET /api/people | mcp__robothor-memory__list_people |
| `update_company` | PATCH /api/companies/{id} | mcp__robothor-memory__update_company |
| `create_note` | POST /api/notes | mcp__robothor-memory__create_note |
| `merge_contacts` | POST /api/people/merge | mcp__robothor-memory__merge_people (alias) |
| `merge_companies` | POST /api/companies/merge | mcp__robothor-memory__merge_companies (alias) |
| `create_task` | POST /api/tasks | mcp__robothor-memory__create_task |
| `update_task` | PATCH /api/tasks/{id} | mcp__robothor-memory__update_task |
| `get_task` | GET /api/tasks/{id} | mcp__robothor-memory__get_task |
| `delete_task` | DELETE /api/tasks/{id} | mcp__robothor-memory__delete_task |
| `list_tasks` | GET /api/tasks | mcp__robothor-memory__list_tasks |
| `list_my_tasks` | GET /api/tasks/agent/{agent_id} | mcp__robothor-memory__list_agent_tasks |
| `resolve_task` | POST /api/tasks/{id}/resolve | mcp__robothor-memory__resolve_task |
| `crm_health` | GET /health | — |
| `vault_get` | *(direct DAL — PostgreSQL vault)* | — |
| `vault_set` | *(direct DAL — PostgreSQL vault)* | — |
| `vault_list` | *(direct DAL — PostgreSQL vault)* | — |
| `vault_delete` | *(direct DAL — PostgreSQL vault)* | — |

Tool names are intentionally identical between MCP and plugin so agent instructions (HEARTBEAT.md, AGENTS.md, jobs.json) work unchanged regardless of runtime.
