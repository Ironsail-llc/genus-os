# Robothor — Project Root

Robothor is an autonomous AI entity — Philip's partner, not an assistant. This directory is the single entry point to understand the entire system.

For Robothor's identity and personality, read `brain/SOUL.md`.

## Identity

| Field | Value |
|-------|-------|
| Email | robothor@ironsail.ai |
| Phone | +1 (413) 408-6025 (Twilio) |
| Voice | Daniel (ElevenLabs) |
| Domain | robothor.ai |
| GCP Project | robothor-485903 |
| Telegram Bot | Robothor (main session delivery) |
| Home | 29 W 16th Road, Broad Channel, NY 11693 |

## System Map

| Path | Real Location | Purpose |
|------|--------------|---------|
| `brain/` | `~/clawd/` | Core workspace: memory, scripts, voice, vision, dashboards, identity |
| `comms/` | `~/moltbot/` | OpenClaw messaging framework (30+ channels, gateway) |
| `runtime/` | `~/.openclaw/` | OpenClaw runtime: agents, cron jobs, credentials |
| `health/` | `~/garmin-sync/` | Garmin health data sync (every 15 min → SQLite → daily memory) |
| `templates/` | `~/clawd-main/` | Bootstrap templates for new Robothor instances |
| `tunnel/` | `~/.cloudflared/` | Cloudflare tunnel config (robothor.ai routes) |
| `crm/` | `robothor/crm/` | CRM stack: Twenty, Chatwoot, Bridge, Docker Compose |

These are symlinks for navigation. All services and crons use absolute paths — nothing breaks.

## Architecture at a Glance

**Three-tier intelligence pipeline feeds RAG. Triage worker processes logs. Supervisor audits and surfaces.**

```
Layer 1: System Crons (Python, crontab)
  calendar_sync.py, email_sync.py, jira_sync.py → JSON logs with null fields
  vision_service.py → YOLO + InsightFace detection loop (systemd)
  garmin_sync.py → health data every 15 min

Layer 1.5: Intelligence Pipeline (3 tiers, all local Llama + pgvector)
  Tier 1: continuous_ingest.py (*/10 min) — incremental deduped ingestion (~10 min freshness)
  Tier 2: periodic_analysis.py (4x daily: 7,11,15,19) — meeting prep, memory blocks, entities
  Tier 3: intelligence_pipeline.py (daily 3:30 AM) — relationships, engagement, patterns, quality

Layer 2a: Triage Worker (Kimi K2.5, */15 min, isolated cron)
  Reads logs → categorizes → handles routine items → escalates complex to worker-handoff.json

Layer 2b: Supervisor Heartbeat (Kimi K2.5, */17 min 7-22h, isolated cron)
  Phase 1: Surface escalations to Philip via Telegram, check health, meeting reminders
  Phase 2: Audit all logs for completeness (reviewedAt, actionCompletedAt, resolvedAt)
  Phase 3: Output audit summary or HEARTBEAT_OK
```

Deep reference: `brain/ARCHITECTURE.md`, `brain/CRON_DESIGN.md`

## Vision System

Always-on service with event-triggered smart detection. Models (YOLO + InsightFace) loaded at startup. Three modes: **disarmed** (idle), **basic** (motion → YOLO → InsightFace → instant Telegram photo alerts + async VLM follow-up), **armed** (same + per-frame tracking). Unknown person → snapshot to Philip's Telegram in <2s. Mode switch at runtime, no restart needed.

| Component | Model | Size |
|-----------|-------|------|
| Object detection | YOLOv8-nano | 6 MB |
| Face recognition | InsightFace buffalo_l (ArcFace) | 300 MB |
| Scene analysis | llama3.2-vision:11b (on-demand) | 7.8 GB |
| Camera | USB webcam → MediaMTX RTSP :8554 + HLS :8890 | 640x480 |

**Service:** `robothor-vision.service` (system-level, needs `sudo`)
**Health:** `http://localhost:8600/health`
**Mode switch:** `curl -X POST http://localhost:8600/mode -d '{"mode":"armed"}'`
**Live stream:** `https://cam.robothor.ai/webcam/` (Cloudflare Access protected)

MCP tools: `look`, `who_is_here`, `enroll_face`, `set_vision_mode`
Orchestrator endpoints: `/vision/{look,detect,identify,status,enroll,mode}` on port 9099

Deep reference: `brain/VISION.md`

## Infrastructure

**Hardware:** Lenovo ThinkStation PGX — NVIDIA Grace Blackwell GB10, 128 GB unified memory, ARM Cortex-X925 (20 cores)

**Networking (Cloudflare Tunnel):**
| Route | Service | Port | Auth |
|-------|---------|------|------|
| cam.robothor.ai | Webcam HLS stream | 8890 | Cloudflare Access (email OTP) |
| robothor.ai | Status server | 3000 | Public |
| status.robothor.ai | Status dashboard | 3001 | Public |
| dashboard.robothor.ai | Status dashboard (alias) | 3001 | Public |
| privacy.robothor.ai | Privacy policy | 3002 | Public |
| ops.robothor.ai | Ops dashboard | 3003 | Cloudflare Access (email OTP) |
| voice.robothor.ai | Voice server (Twilio) | 8765 | Public |
| sms.robothor.ai | SMS webhook (Twilio) | 8766 | Public |
| gateway.robothor.ai | OpenClaw gateway | 18789 | Cloudflare Access (email OTP) |
| crm.robothor.ai | Twenty CRM | 3030 | Cloudflare Access (email OTP) |
| inbox.robothor.ai | Chatwoot inbox | 3100 | Cloudflare Access (email OTP) |
| bridge.robothor.ai | Bridge service | 9100 | Cloudflare Access (email OTP) |
| orchestrator.robothor.ai | RAG Orchestrator | 9099 | Cloudflare Access (email OTP) |
| vision.robothor.ai | Vision API | 8600 | Cloudflare Access (email OTP) |
| monitor.robothor.ai | Uptime Kuma | 3010 | Cloudflare Access (email OTP) |
| vault.robothor.ai | Vaultwarden | 8222 | Cloudflare Access (email OTP) |
| app.robothor.ai | Business layer | 3004 | Cloudflare Access (email OTP) |
| Tailscale IP | 100.91.221.100 (ironsail tailnet) | — | — |

All camera ports bound to `127.0.0.1`. Webcam only accessible externally via `cam.robothor.ai` (Cloudflare Access: `philip@ironsail.ai`, `robothor@ironsail.ai`).

**Database:** PostgreSQL 16 + pgvector 0.6.0 (max_connections=200, Docker-accessible via 172.17.0.1)
Databases: `robothor_memory` (facts, entities, contacts, memory blocks), `twenty_crm`, `chatwoot`, `vaultwarden`
Key tables: `memory_facts`, `memory_entities`, `memory_relations`, `contact_identifiers`, `agent_memory_blocks`

**Redis:** Port 6379, maxmemory 2GB. Shared by Twenty CRM, Chatwoot, RAG orchestrator.

**Ollama Models (localhost:11434):**
| Model | Size | Role |
|-------|------|------|
| llama3.2-vision:11b | 7.8 GB | Vision analysis, intelligence pipeline |
| qwen3-embedding:0.6b | 639 MB | Dense vector embeddings (1024-dim) |
| Qwen3-Reranker-0.6B:F16 | 1.2 GB | Cross-encoder reranking |

Note: qwen3-next (80B, RAG generation) is loaded on demand — not always resident.

Deep reference: `INFRASTRUCTURE.md`

## Services & Ports

| Service | Unit | Level | Port | Notes |
|---------|------|-------|------|-------|
| Vision service | robothor-vision.service | system (`sudo`) | 8600 | Vision: disarmed/basic/armed modes |
| MediaMTX | mediamtx-webcam.service | system (`sudo`) | 8554, 8890 | USB webcam → RTSP + HLS |
| RAG Orchestrator | robothor-orchestrator.service | system (`sudo`) | 9099 | FastAPI, RAG + vision endpoints |
| Voice server | robothor-voice.service | system (`sudo`) | 8765 | Twilio ConversationRelay |
| SMS webhook | robothor-sms.service | system (`sudo`) | 8766 | Twilio SMS webhooks |
| Status server | robothor-status.service | system (`sudo`) | 3000 | robothor.ai homepage |
| Status dashboard | robothor-status-dashboard.service | system (`sudo`) | 3001 | status.robothor.ai |
| Ops dashboard | robothor-dashboard.service | system (`sudo`) | 3003 | ops.robothor.ai |
| Privacy policy | robothor-privacy.service | system (`sudo`) | 3002 | privacy.robothor.ai |
| Transcript watcher | robothor-transcript.service | system (`sudo`) | — | Watches voice transcripts |
| Cloudflare tunnel | cloudflared.service | system (`sudo`) | — | robothor.ai routes |
| CRM stack | robothor-crm.service | system (`sudo`) | 3030, 3100 | Docker: Twenty CRM + Chatwoot (4 containers) |
| Bridge service | robothor-bridge.service | system (`sudo`) | 9100 | Contact resolution, webhooks, CRM integration |
| Vaultwarden | (Docker in robothor-crm) | Docker | 8222 | Password vault (vault.robothor.ai) |
| Uptime Kuma | (Docker in robothor-crm) | Docker | 3010 | Service monitoring dashboard |
| Business layer | robothor-app.service | system (`sudo`) | 3004 | Next.js + CopilotKit (app.robothor.ai) |
| Samba | smbd.service, nmbd.service | system | 445 | Network file shares (local + Tailscale only) |
| Tailscale | tailscaled.service | system (`sudo`) | — | VPN mesh (ironsail tailnet) |
| Moltbot gateway | moltbot-gateway.service | system (`sudo`) | 18789 | OpenClaw messaging |

Deep reference: `SERVICES.md`

## Memory System

Three-tier architecture with structured facts and entity graph:

- **Working memory:** Context window (current session)
- **Short-term:** PostgreSQL, 48h TTL, auto-decays
- **Long-term:** PostgreSQL + pgvector, permanent, importance-scored

**Structured layers:**
- `memory_facts` — categorized facts with confidence, lifecycle, conflict resolution
- `memory_entities` + `memory_relations` — knowledge graph (people, projects, tech)

**Structured working memory:**
- `agent_memory_blocks` — named text blocks (persona, user_profile, working_context, operational_findings, contacts_summary) with size limits and usage tracking

**RAG stack:** Qwen3-Embedding → pgvector → Qwen3-Reranker → Qwen3-Next (generation)

**MCP tools (robothor-memory server):** — Claude Code sessions
- `search_memory`, `store_memory`, `get_stats`, `get_entity` — facts + knowledge graph
- `memory_block_read`, `memory_block_write`, `memory_block_list` — structured working memory
- `log_interaction` — CRM interaction logging (→ bridge → Chatwoot + Twenty)
- `look`, `who_is_here`, `enroll_face`, `set_vision_mode` — vision

**MCP tools (twenty-crm server):** `list_people`, `get_person`, `create_person`, `update_person`, `search_records`, `create_note`, etc.
**MCP tools (chatwoot server):** `chatwoot_list_conversations`, `chatwoot_get_conversation`, `chatwoot_list_messages`, `chatwoot_create_message`
**MCP tools (notebooklm server):** — Google NotebookLM research notebooks (on-demand via `uvx`)
- `notebook_create`, `notebook_list`, `notebook_get`, `notebook_delete`, `notebook_rename` — notebook management
- `source_add`, `source_list`, `source_get`, `source_delete`, `source_content` — add URLs, Drive docs, text, or files as sources
- `notebook_query` — ask questions against notebook sources (AI-powered Q&A)
- `research_start` — discover and add sources from the web on a topic
- `studio_create`, `studio_list`, `studio_get`, `studio_delete` — generate artifacts (audio overviews, reports, quizzes, flashcards, mind maps, slides, infographics, videos, data tables)
- `audio_create`, `report_create`, `quiz_create`, `video_create` — shortcut artifact creation
- `share_notebook`, `export_artifact` — sharing and Google Docs/Sheets export
- `download` — download generated audio/video files
- **Auth:** Google cookies via `nlm login` (browser-based). Cookies stored at `~/.notebooklm-mcp-cli/profiles/default/auth.json`. Expire every 2-4 weeks — re-run `nlm login` to renew.

**OpenClaw plugin (crm-tools):** — OpenClaw agent sessions (cron jobs, Telegram, Google Chat)
Same tool names as MCP (`chatwoot_list_conversations`, `log_interaction`, `create_person`, `create_note`, etc.) but routed through Bridge REST proxy on :9100. Agent instructions work identically in both runtimes.

**Ingestion:** `POST /ingest` on port 9099 (channels: discord, email, cli, api, telegram, camera)

Deep reference: `brain/memory_system/MEMORY_SYSTEM.md`

## CRM Stack

**Twenty CRM** (port 3030) — Contact/company/relationship store. Replaces `contacts.json`.
**Chatwoot** (port 3100) — Unified conversation inbox across all channels.
**Bridge** (port 9100) — Glue service: contact resolution, webhooks, data sync.

All run as Docker containers via `robothor-crm.service`. Bridge runs as native Python via `robothor-bridge.service`.

Cross-system identity: `contact_identifiers` table maps channel+identifier → Twenty person ID + Chatwoot contact ID + memory entity ID.

**CRM tool access:** Claude Code uses MCP servers (stdio, direct API). OpenClaw agent sessions use the `crm-tools` plugin (HTTP via Bridge :9100 proxy). Tool names are identical in both — agent instructions work unchanged.

Web UIs: `crm.robothor.ai`, `inbox.robothor.ai` (Cloudflare Access protected)

Deep reference: `crm/` directory, `INFRASTRUCTURE.md`

## Backup

**LUKS-encrypted SanDisk SSD** (1.8 TB) at `/mnt/robothor-backup`, auto-unlocked via `/etc/crypttab` keyfile.

| Field | Value |
|-------|-------|
| Device | /dev/sda1 (LUKS2) |
| Mount | /mnt/robothor-backup |
| Keyfile | /root/robothor-backup.key (slot 0) |
| Passphrase | Slot 1 fallback (stored in memory system) |
| Schedule | Daily 4:30 AM |
| Script | `scripts/backup-ssd.sh` |
| Log | `scripts/backup.log` |
| Retention | 30 days (DB dumps) |

**What's backed up:** All project dirs (`clawd`, `moltbot`, `garmin-sync`, `clawd-main`, `robothor`), config dirs (`.openclaw`, `.cloudflared`), systemd service files, credentials (`.bashrc`, `crm/.env`), 4x PostgreSQL dumps (`robothor_memory`, `twenty_crm`, `chatwoot`, `vaultwarden`), Docker volumes (`crm_twenty-server-data`, `crm_twenty-docker-data`, `crm_chatwoot-storage`, `crm_vaultwarden-data`), crontab + ollama model list, verification manifest.

Deep reference: `scripts/backup-ssd.sh`

## Secrets Management

**SOPS + age** — all credentials encrypted at rest, decrypted at runtime.

| Component | Location |
|-----------|----------|
| Age private key | `/etc/robothor/age.key` (root:philip 640) |
| Encrypted secrets | `/etc/robothor/secrets.enc.json` (root:philip 640) |
| Decrypted at runtime | `/run/robothor/secrets.env` (tmpfs, created by decrypt-secrets.sh) |
| SOPS config | `/etc/robothor/.sops.yaml` |
| Cron wrapper | `scripts/cron-wrapper.sh` (sources secrets.env before exec) |
| Systemd wrapper | `scripts/decrypt-secrets.sh` (ExecStartPre in services) |

**How services get credentials:**
- Systemd services: `ExecStartPre=decrypt-secrets.sh` + `EnvironmentFile=/run/robothor/secrets.env`
- Cron jobs: wrapped with `cron-wrapper.sh` which sources `/run/robothor/secrets.env`
- Python scripts: `os.environ["KEY_NAME"]` (no hardcoded values)
- Docker Compose: reads `crm/.env` directly (Docker env_file)

**Adding/rotating a secret:**
1. Decrypt: `sudo SOPS_AGE_KEY_FILE=/etc/robothor/age.key sops /etc/robothor/secrets.enc.json`
2. Edit the value in the JSON editor
3. Save — SOPS re-encrypts automatically
4. Restart affected services: `sudo systemctl restart <service>`

**Pre-commit hook:** `gitleaks` scans staged changes for leaked secrets before every commit.

## Monitoring

**Uptime Kuma** — HTTP/TCP health checks for all services, Telegram alerts on downtime.

- **URL:** `https://monitor.robothor.ai` (Cloudflare Access protected)
- **Container:** `uptime-kuma` in `crm/docker-compose.yml`
- **Port:** 3010 (mapped from container's 3001)
- **Data:** Docker volume `crm_uptime-kuma-data`

## Key Memory Files

| File | Purpose |
|------|---------|
| `brain/memory/email-log.json` | Processed emails with urgency and notifier fields |
| `brain/memory/calendar-log.json` | Calendar events with absolute timestamps |
| `brain/memory/jira-log.json` | Jira ticket sync |
| `brain/memory/worker-handoff.json` | Escalations from triage worker → supervisor |
| `brain/memory/tasks.json` | Central task list |
| `brain/memory/contacts.json` | Contact profiles |
| `brain/memory/security-log.json` | Security events |
| `brain/memory/YYYY-MM-DD.md` | Daily notes (raw session logs) |
| `brain/MEMORY.md` | Curated long-term memory |

## Cron Schedule

**System crontab (Layer 1 — Python, mechanical, no AI):**
| Schedule | Job | Script |
|----------|-----|--------|
| */5 * * * * | Calendar sync | `brain/scripts/calendar_sync.py` |
| */5 * * * * | Email sync | `brain/scripts/email_sync.py` |
| */30 6-22 * * 1-5 | Jira sync | `brain/scripts/jira_sync.py` |
| */15 * * * * | Garmin sync | `health/garmin_sync.py` |
| 0 3 * * * | Memory maintenance | `brain/memory_system/maintenance.sh` |
| */10 * * * * | Continuous ingestion (Tier 1) | `brain/memory_system/continuous_ingest.py` |
| 0 7,11,15,19 * * * | Periodic analysis (Tier 2) | `brain/memory_system/periodic_analysis.py` |
| 30 3 * * * | Deep analysis (Tier 3) | `brain/memory_system/intelligence_pipeline.py` |
| 0 4 * * * | Snapshot cleanup (>30d) | `find` + delete |
| 30 4 * * * | SSD backup (daily) | `scripts/backup-ssd.sh` |

**OpenClaw crons (Layer 2 — Kimi K2.5 via OpenRouter, via `runtime/cron/jobs.json`):**
| Schedule | Job | Purpose |
|----------|-----|---------|
| */15 * * * * | Triage Worker | Process logs, categorize, act, escalate |
| */17 7-22 * * * | Supervisor Heartbeat | Surface escalations, audit logs |
| */10 7-23 * * * | Vision Monitor | Check motion events, alert on visitors |
| 30 6 * * * | Morning Briefing | Daily briefing (calendar, email, weather) |
| 0 21 * * * | Evening Wind-Down | Tomorrow preview, open items |

Deep reference: `brain/CRON_DESIGN.md`, `docs/CRON_MAP.md`

## Documentation Index

| Document | Location | Purpose |
|----------|----------|---------|
| CLAUDE.md | `robothor/CLAUDE.md` | **This file** — project root, single entry point |
| INFRASTRUCTURE.md | `robothor/INFRASTRUCTURE.md` | Hardware, networking, database, models |
| SERVICES.md | `robothor/SERVICES.md` | All systemd services, health checks, cron schedule |
| DATA_FLOW.md | `robothor/docs/DATA_FLOW.md` | End-to-end data flow from APIs to Philip |
| CRON_MAP.md | `robothor/docs/CRON_MAP.md` | Unified cron schedule and timeline |
| ARCHITECTURE.md | `brain/ARCHITECTURE.md` | Three-layer architecture, data flow diagrams |
| VISION.md | `brain/VISION.md` | Vision system: modes, API, face enrollment, remote access |
| TOOLS.md | `brain/TOOLS.md` | Tools reference: models, APIs, credentials, Cloudflare |
| CRON_DESIGN.md | `brain/CRON_DESIGN.md` | Cron architecture and design principles |
| SOUL.md | `brain/SOUL.md` | Robothor's identity and personality |
| AGENTS.md | `brain/AGENTS.md` | Agent configuration and startup instructions |
| MEMORY.md | `brain/MEMORY.md` | Curated long-term memory (operational findings) |
| MEMORY_SYSTEM.md | `brain/memory_system/MEMORY_SYSTEM.md` | Memory system: RAG, facts, entities, ingestion |
| TESTING.md | `docs/TESTING.md` | Testing strategy, AI test patterns, 6-phase coverage plan |

## Cloudflare Credentials

**Account:** `cloudflare@valhallavitality.com` (dashboard login in SOPS as `CLOUDFLARE_EMAIL`)

Three API tokens for managing the Cloudflare tunnel, DNS, and Access:

| Token Name | SOPS Key | Permission | Use For |
|------------|----------|-----------|---------|
| `robothor-tunnel-edit` | `CLOUDFLARE_API_TOKEN` | Tunnel Edit | Adding/modifying tunnel ingress routes |
| `robothor-dns-edit` | `CLOUDFLARE_DNS_TOKEN` | Zone DNS Edit/Read | Creating DNS CNAME records for new subdomains |
| `robothor-access-edit` | `CLOUDFLARE_ACCESS_TOKEN` | Access Apps/Policies Edit | Managing Zero Trust access policies |

All token values stored in SOPS (`/etc/robothor/secrets.enc.json`). Available at runtime via `/run/robothor/secrets.env`.
Zone ID: `ebd618c6c9edda6ec86f5168daeb8240`

## Task-Specific Reading Guide

| Task | Read first |
|------|-----------|
| Working on vision | `brain/VISION.md` |
| Viewing the webcam | `https://cam.robothor.ai/webcam/` (Cloudflare Access) |
| Changing cron behavior | `brain/CRON_DESIGN.md` + `runtime/cron/jobs.json` |
| Understanding memory/RAG | `brain/memory_system/MEMORY_SYSTEM.md` |
| Sending emails or calendar | `brain/TOOLS.md` (gog CLI section) |
| Voice calling | `brain/TOOLS.md` (voice section) + `brain/voice-server/` |
| Cloudflare tunnel routes | `brain/TOOLS.md` (Cloudflare section) |
| Adding new tunnel subdomain | `brain/TOOLS.md` (Cloudflare section — 4-step workflow) |
| OpenClaw agents/messaging | `comms/` README + `runtime/` config files |
| Robothor's identity | `brain/SOUL.md` |
| Model selection | `brain/TOOLS.md` (Model Selection Guide) |
| Session startup (as Robothor) | `brain/AGENTS.md` |
| Health data | `health/` + `brain/memory/garmin-health.md` |
| CRM / contacts / conversations | `crm/` directory + `INFRASTRUCTURE.md` (CRM Stack section) |
| Bridge service / webhooks | `crm/bridge/bridge_service.py` |
| Contact resolution | `crm/bridge/contact_resolver.py` |
| Memory blocks | `brain/AGENTS.md` (Memory Blocks section) |
| Services & ports | `SERVICES.md` |
| Hardware & infrastructure | `INFRASTRUCTURE.md` |
| Writing or running tests | `docs/TESTING.md` + `brain/memory_system/conftest.py` |
| Backup / SSD / restore | `scripts/backup-ssd.sh` + `INFRASTRUCTURE.md` (External Storage) |
| Research notebooks (NotebookLM) | `nlm --help` (CLI) — auth: `nlm login`, check: `nlm login --check` |

## Rules

1. **Don't move directories** — all services and crons use absolute paths to `~/clawd/`, `~/.openclaw/`, etc. Symlinks here are for navigation only.
2. **Don't commit secrets** — API keys live in `runtime/` and environment variables, never in git.
3. **comms/ is a public repo** — `~/moltbot/` is open source OpenClaw. Don't put private data there.
4. **Vision service needs `sudo`** — `sudo systemctl {start,stop,restart,status} robothor-vision`
5. **All system-level services need `sudo`** — everything in `/etc/systemd/system/` requires sudo.
6. **All services are system-level** — no user-level systemd services. Use `sudo systemctl` for everything.
7. **Crons must have `delivery: announce`** — crons with `delivery: none` silently don't run.
8. **Model: Kimi K2.5** (via OpenRouter) for all agent/interactive work. Opus 4.6 is first fallback. Local Qwen3 for RAG generation only.
9. **No localhost URLs in agent instructions** — Agent-facing docs (HEARTBEAT.md, WORKER.md, AGENTS.md, jobs.json) must never reference `localhost` or `127.0.0.1` URLs. OpenClaw's `web_fetch` tool blocks loopback addresses for security. Use the appropriate registered tools instead (e.g. `crm_health` instead of fetching `localhost:9100/health`). Localhost is fine in internal code (plugins, Python scripts) and infrastructure reference docs.
10. **All services with ports must have Cloudflare tunnel routes** — Internal/sensitive services must use Cloudflare Access (email OTP). Public-facing services (status, voice, gchat, privacy) are unprotected.
11. **Test before commit** — New functions, endpoints, and features require tests. Bug fixes require a regression test that reproduces the bug first. Run fast tests before commit: `pytest -m "not slow and not llm and not e2e"`.
12. **Use pytest markers** — No marker = unit test (<1s, mocked deps). `@pytest.mark.integration` = real DB/Redis. `@pytest.mark.llm` = needs Ollama. `@pytest.mark.slow` = >10s. `@pytest.mark.e2e` = all services running. `@pytest.mark.smoke` = health checks. Use `test_prefix` fixture for data isolation.
13. **Test AI by properties, not values** — Validate structure (fields present, types correct, values in valid ranges), not exact content. Golden datasets for regression (80%+ match). Mock LLMs in unit tests.
14. **Tests live alongside code** — `<module>/tests/test_<feature>.py` with `conftest.py`. Gold standard: `brain/memory_system/conftest.py`.
15. **Service test requirements** — FastAPI: `httpx.AsyncClient` + `ASGITransport`. MCP: tool schema validation. Systemd: smoke test (is-active + health 200). Cron: `freezegun` + output structure validation.
16. **Never commit credentials** — All secrets live in SOPS-encrypted `/etc/robothor/secrets.enc.json`. Use `os.getenv()` in Python, `$VAR` in shell. The gitleaks pre-commit hook blocks commits containing secrets.

## Server Infrastructure Policy

This machine runs 24/7. Every long-running process is managed by a system-level systemd service, enabled on boot. There are no user-level systemd services.

**Rules:**
- Every service with a listening port gets a Cloudflare tunnel route
- Internal/sensitive services (CRM, bridge, orchestrator, vision, ops dashboard) are protected with Cloudflare Access (email OTP for philip@ironsail.ai and robothor@ironsail.ai)
- Public services (status, voice, gchat, privacy) have no auth gate
- SearXNG (:8888) is the exception — internal-only search engine, no tunnel route
- All services use `Restart=always` and `RestartSec=5`
- All services use `KillMode=control-group` (not `process`) to prevent orphaned children

## Testing Policy

**Markers:**
| Marker | Meaning | Speed |
|--------|---------|-------|
| *(none)* | Unit test — mocked deps, no I/O | <1s |
| `@pytest.mark.integration` | Real DB/Redis | <10s |
| `@pytest.mark.llm` | Needs Ollama running | varies |
| `@pytest.mark.slow` | >10s wall time | >10s |
| `@pytest.mark.e2e` | Full system end-to-end | >30s |
| `@pytest.mark.smoke` | Health check only | <3s |

**Run commands:**
| Context | Command |
|---------|---------|
| Pre-commit (fast) | `pytest -m "not slow and not llm and not e2e"` |
| Pre-push | `bash run_tests.sh` |
| Full suite | `bash run_tests.sh --all` |
| Single module | `pytest crm/bridge/tests/ -v` |

**Gold standard:** `brain/memory_system/conftest.py` — test_prefix isolation, autouse cleanup, layered runner.

Deep reference: `docs/TESTING.md`
