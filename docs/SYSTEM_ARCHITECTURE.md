# Robothor — System Architecture

> Technical reference for the Robothor autonomous AI platform.
> Last updated: 2026-02-20

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Hardware & Infrastructure](#hardware--infrastructure)
3. [Architecture Overview](#architecture-overview)
4. [Service Topology](#service-topology)
5. [Network Edge — Cloudflare Tunnel](#network-edge--cloudflare-tunnel)
6. [Data Layer](#data-layer)
7. [Intelligence Pipeline](#intelligence-pipeline)
8. [Triage & Heartbeat Pipeline](#triage--heartbeat-pipeline)
9. [Vision System](#vision-system)
10. [CRM Stack](#crm-stack)
11. [Memory System](#memory-system)
12. [Communications Layer](#communications-layer)
13. [Tool Access Topology](#tool-access-topology)
14. [Cron Schedule](#cron-schedule)
15. [Backup & Recovery](#backup--recovery)
16. [Folder Structure](#folder-structure)

---

## Executive Summary

Robothor is an autonomous AI entity running 24/7 on dedicated hardware. It manages Philip's communications, calendar, contacts, security monitoring, and knowledge base — acting as a partner rather than an assistant.

**Core capabilities:**

- Always-on vision surveillance with face recognition and instant Telegram alerts
- Three-tier intelligence pipeline: ingest (10 min) → analysis (4x/day) → deep synthesis (daily)
- Unified CRM across email, Telegram, Google Chat, SMS, voice, and video meetings
- RAG-powered memory with structured facts, entity graph, and working memory blocks
- Autonomous triage: categorizes, handles routine items, escalates complex ones
- Voice calling and SMS via Twilio, Telegram delivery via Python Agent Engine

**Key constraints:**

- Single-machine deployment (no cloud compute)
- All services managed by systemd (system-level, `Restart=always`)
- All external access via Cloudflare Tunnel (no open ports)
- LLM inference is local (Ollama) for embeddings/reranking/vision; remote (OpenRouter) for agent work

---

## Hardware & Infrastructure

```
┌──────────────────────────────────────────────────────────────────┐
│  Lenovo ThinkStation PGX                                         │
│                                                                  │
│  CPU:    ARM Cortex-X925 (20 cores)                              │
│  GPU:    NVIDIA Grace Blackwell GB10                             │
│  Memory: 128 GB unified                                          │
│  OS:     Ubuntu Linux 6.14.0-1015-nvidia (ARM64)                 │
│  VPN:    Tailscale 100.91.221.100 (ironsail tailnet)             │
└──────────────────────────────────────────────────────────────────┘
         │                                    │
    USB Webcam (640x480)               SanDisk SSD 1.8 TB
    → MediaMTX RTSP/HLS               LUKS2-encrypted
                                       /mnt/robothor-backup
```

| Component | Details |
|-----------|---------|
| Database | PostgreSQL 16 + pgvector 0.6.0 (max_connections=200) |
| Cache | Redis 6379, maxmemory 2 GB |
| Search | SearXNG :8888 (internal only, no tunnel) |
| Container runtime | Docker (rootful, accessed via `sudo`) |

### Local AI Models (Ollama, localhost:11434)

| Model | Size | Role | Residency |
|-------|------|------|-----------|
| qwen3-embedding:0.6b | 639 MB | Dense vector embeddings (1024-dim) | Always loaded |
| Qwen3-Reranker-0.6B:F16 | 1.2 GB | Cross-encoder reranking | Always loaded |
| llama3.2-vision:11b | 7.8 GB | Vision analysis, intelligence pipeline | Always loaded |
| qwen3-next:80B | ~48 GB | RAG generation | On-demand |

### Remote AI Models (OpenRouter)

| Model | Role |
|-------|------|
| Kimi K2.5 | Triage worker, cron agent jobs |
| Claude Opus 4.6 | Fallback for agent work, Claude Code sessions |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL WORLD                                 │
│  Google (Calendar, Gmail, Drive, Meet)  ·  Jira  ·  Garmin  ·  Twilio  │
│  Telegram  ·  Google Chat  ·  SMS  ·  Voice calls  ·  Webcam visitors  │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   Cloudflare Tunnel      │
                    │   (all external access)  │
                    └────────────┬────────────┘
                                 │
┌────────────────────────────────┼────────────────────────────────────────┐
│                         SERVICE LAYER                                   │
│                                                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │ Vision   │ │ Voice    │ │ SMS      │ │ Engine   │ │ Bridge   │     │
│  │ :8600    │ │ :8765    │ │ :8766    │ │ :18800   │ │ :9100    │     │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘     │
│       │             │            │             │            │           │
│  ┌────┴─────────────┴────────────┴─────────────┴────────────┴─────┐    │
│  │                    RAG Orchestrator :9099                       │    │
│  │           /ingest  ·  /query  ·  /vision/*                     │    │
│  └────────────────────────────┬───────────────────────────────────┘    │
│                               │                                        │
│  ┌────────────────────────────┴───────────────────────────────────┐    │
│  │                     DATA LAYER                                  │    │
│  │  PostgreSQL 16 + pgvector  ·  Redis  ·  Ollama (local LLMs)   │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                  INTELLIGENCE PIPELINE                           │   │
│  │  Tier 1: continuous_ingest (*/10)                                │   │
│  │  Tier 2: periodic_analysis (4x/day)                              │   │
│  │  Tier 3: intelligence_pipeline (daily)                           │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                  TRIAGE PIPELINE (Kimi K2.5)                     │   │
│  │  prep → worker (*/15) → cleanup → relay → heartbeat (4h)        │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌──────────────────────────────────┐  ┌──────────────┐                  │
│  │ CRM (native PostgreSQL tables)  │  │  Web UIs     │                  │
│  │ crm_* in robothor_memory        │  │ :3000-3003   │                  │
│  │                                  │  │ (Node.js)    │                  │
│  └──────────────────────────────────┘  └──────────────┘                  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Service Topology

All services are **system-level systemd units** (`/etc/systemd/system/`), managed with `sudo systemctl`. Every service uses `Restart=always`, `RestartSec=5`, `KillMode=control-group`.

| Service | Unit | Port | Technology | Purpose |
|---------|------|------|------------|---------|
| Vision | robothor-vision.service | 8600 | Python/FastAPI | YOLO + InsightFace + VLM detection |
| MediaMTX | mediamtx-webcam.service | 8554, 8890 | Go binary | USB webcam → RTSP + HLS |
| RAG Orchestrator | robothor-orchestrator.service | 9099 | Python/FastAPI | RAG queries, ingestion API, vision proxy |
| Voice | robothor-voice.service | 8765 | Python | Twilio ConversationRelay + ElevenLabs |
| SMS | robothor-sms.service | 8766 | Python | Twilio SMS webhooks |
| Status page | robothor-status.service | 3000 | Node.js | robothor.ai homepage |
| Dashboard | robothor-status-dashboard.service | 3001 | Node.js | status.robothor.ai |
| Ops dashboard | robothor-dashboard.service | 3003 | Node.js | ops.robothor.ai |
| Privacy policy | robothor-privacy.service | 3002 | Node.js | privacy.robothor.ai |
| CRM stack | robothor-crm.service | 3010, 8222, 8880 | Docker Compose | Vaultwarden, Uptime Kuma, Kokoro TTS |
| Bridge | robothor-bridge.service | 9100 | Python/FastAPI | Contact resolution, webhooks, REST proxy |
| Agent Engine | robothor-engine.service | 18800 | Python/FastAPI | Agent orchestration, Telegram, cron scheduler |
| Transcript watcher | robothor-transcript.service | — | Python | Voice transcript processing |
| Tunnel | cloudflared.service | — | Go binary | Cloudflare Tunnel (all external routing) |
| VPN | tailscaled.service | — | Go binary | Tailscale mesh (ironsail tailnet) |

---

## Network Edge — Cloudflare Tunnel

All external access routes through a single Cloudflare Tunnel. No ports are exposed directly to the internet.

### Public Routes (no authentication)

| Subdomain | Port | Service |
|-----------|------|---------|
| robothor.ai | 3000 | Status homepage |
| status.robothor.ai | 3001 | Status dashboard |
| dashboard.robothor.ai | 3001 | Status dashboard (alias) |
| privacy.robothor.ai | 3002 | Privacy policy |
| voice.robothor.ai | 8765 | Twilio voice webhooks |
| sms.robothor.ai | 8766 | Twilio SMS webhooks |

### Protected Routes (Cloudflare Access — email OTP)

Authorized emails: `philip@ironsail.ai`, `robothor@ironsail.ai`

| Subdomain | Port | Service |
|-----------|------|---------|
| cam.robothor.ai | 8890 | Live webcam HLS stream |
| ops.robothor.ai | 3003 | Ops dashboard |
| bridge.robothor.ai | 9100 | Bridge API |
| engine.robothor.ai | 18800 | Agent Engine API |
| orchestrator.robothor.ai | 9099 | RAG orchestrator API |
| vision.robothor.ai | 8600 | Vision API |

### Network Topology

```
Internet → Cloudflare Edge → Tunnel (cloudflared) → localhost:<port>
                                                         │
                                              All camera ports bound
                                              to 127.0.0.1 only
```

Docker containers reach host services via `172.17.0.1` (Docker bridge). PostgreSQL listens on Docker bridge in addition to localhost. Redis runs with `protected-mode off` for Docker access.

---

## Data Layer

### PostgreSQL 16 + pgvector 0.6.0

Two databases on the same instance:

| Database | Owner | Purpose |
|----------|-------|---------|
| `robothor_memory` | philip | Facts, entities, contacts, memory blocks, ingestion state, CRM data |
| `vaultwarden` | philip | Vaultwarden password vault |

**Key tables in `robothor_memory`:**

| Table | Purpose |
|-------|---------|
| `memory_facts` | Categorized facts with confidence, lifecycle, embeddings (1024-dim) |
| `memory_entities` | Knowledge graph nodes (people, projects, tech) |
| `memory_relations` | Knowledge graph edges |
| `contact_identifiers` | Cross-system identity: channel+identifier → person_id + entity ID |
| `agent_memory_blocks` | 5 named text blocks (persona, user\_profile, working\_context, operational\_findings, contacts\_summary) |
| `ingestion_watermarks` | Per-source ingestion state for dedup |
| `ingested_items` | Item-level dedup (content hashes) |
| `crm_people` | CRM contacts |
| `crm_companies` | CRM companies |
| `crm_notes` | CRM notes |
| `crm_tasks` | CRM tasks |
| `crm_conversations` | CRM conversations |
| `crm_messages` | CRM messages |

### Redis

Port 6379, 2 GB max. Shared by:
- RAG orchestrator (query cache)

---

## Intelligence Pipeline

Three-tier architecture converts raw API data into structured knowledge:

```
  External APIs              System Crons (Layer 1)              JSON Logs
  ─────────────              ──────────────────────              ─────────
  Google Calendar ──────→ calendar_sync.py (*/5 min) ──────→ calendar-log.json
  Gmail ────────────────→ email_sync.py (*/5 min) ────────→ email-log.json
  Jira ─────────────────→ jira_sync.py (*/30 M-F) ───────→ jira-log.json
  Garmin ───────────────→ garmin_sync.py (*/15 min) ──────→ garmin-health.md
  Google Drive ─────────→ meet_transcript_sync.py (*/10) ─→ meet-transcripts.json
```

### Tier 1 — Continuous Ingestion (every 10 minutes)

`continuous_ingest.py` reads JSON logs incrementally, deduplicates via content hashes, and ingests into pgvector.

- ~10 minute freshness from API event to searchable fact
- Sources: email, calendar, Jira, Meet transcripts, CRM conversations, CRM updates
- Dedup: `ingested_items` table (content\_hash) + `ingestion_watermarks` (per-source cursor)

### Tier 2 — Periodic Analysis (4x daily: 07:00, 11:00, 15:00, 19:00)

`periodic_analysis.py` runs four phases:

1. **Meeting prep** — Briefs for upcoming meetings (participants, recent context, open items)
2. **Memory block updates** — Refreshes the 5 structured working memory blocks
3. **Entity extraction** — Discovers new people, projects, technologies from recent facts
4. **Contact reconciliation & discovery** — Fuzzy name matching to link memory entities to CRM contacts; creates CRM records for high-mention entities (>=5 mentions) and meeting attendees

### Tier 3 — Deep Analysis (daily, 03:30)

`intelligence_pipeline.py` performs:

1. **Relationship mapping** — Strength and recency of connections between entities
2. **Contact enrichment** — Email domain → company lookup, LLM-inferred job titles and cities
3. **Engagement scoring** — Who is Philip interacting with most, and through which channels
4. **Pattern detection** — Recurring topics, communication trends
5. **Data quality** — Stale facts, orphaned entities, confidence decay

### Weekly Synthesis (Sunday 05:00)

`weekly_review.py` produces a deep synthesis document (`weekly-review-YYYY-MM-DD.md`) covering the full week's activity, themes, and recommendations.

```
                    ┌────────────────────────────────┐
                    │         pgvector Store          │
  Tier 1 ────────→  │  memory_facts (embeddings)      │
  (*/10 min)        │  memory_entities                 │
                    │  memory_relations                │
                    └───────────┬────────────────────┘
                                │
  Tier 2 ──────────────────────►│ (enrich, link, discover)
  (4x daily)                    │
                                │
  Tier 3 ──────────────────────►│ (relationships, patterns, quality)
  (daily 3:30 AM)              │
                                │
  Weekly ──────────────────────►│ (deep synthesis → markdown report)
  (Sunday 5 AM)
```

---

## Triage & Heartbeat Pipeline

Converts raw log data into prioritized actions, with an LLM gatekeeper controlling what reaches Philip.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  Layer 1.5: triage_prep.py (runs at :14, :29, :44, :59)        │
  │  - Extracts pending/unprocessed items from JSON logs            │
  │  - Enriches with contact context from PostgreSQL                │
  │  - Outputs: triage-inbox.json (small, focused)                  │
  └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  Layer 2: Triage Worker (Kimi K2.5, */15 via Engine)             │
  │  - Reads triage-inbox.json                                      │
  │  - Categorizes: routine / needs-attention / escalate            │
  │  - Handles routine items autonomously                           │
  │  - Writes triage-status.md (summary for supervisor)             │
  │  - Escalations → worker-handoff.json                            │
  └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  Layer 2.5: triage_cleanup.py (runs at :05, :20, :35, :50)     │
  │  - Marks processed items in source logs                         │
  │  - Updates heartbeat timestamp (prevents false stale alerts)    │
  └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  Layer 3: supervisor_relay.py (*/10, 06:00–23:00)               │
  │  - Meeting alerts → Telegram (the ONLY automated Telegram path) │
  │  - Stale worker / CRM health issues → handoff.json (not Telegram│
  │  - Respects waking hours (07:00–22:00 ET for stale/CRM alerts) │
  │  - Cooldowns: stale=60 min, CRM=30 min                         │
  └──────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  Layer 3.5: Main Heartbeat (Sonnet 4.6, 4h 6-22, TELEGRAM)      │
  │  - Runs ON TELEGRAM (direct channel to Philip)                  │
  │  - Reads *-status.md + worker-handoff.json                      │
  │  - Investigates before surfacing (no raw log dumps)             │
  │  - Sole gatekeeper: decides what's worth Philip's attention     │
  │  - Audits all logs for completeness                             │
  └─────────────────────────────────────────────────────────────────┘
```

**Design principles:**
- Main heartbeat never sends directly to Telegram via API — it runs as a Telegram agent session
- Python relay is the only script that calls the Telegram Bot API (meeting alerts only)
- Only 3 Engine jobs deliver to Telegram: Morning Briefing, Evening Wind-Down, SMS Status Check
- Calendar items older than 24h auto-expire in triage_prep

---

## Vision System

Always-on computer vision with three operational modes:

| Mode | Behavior |
|------|----------|
| **disarmed** | Camera streams but no processing |
| **basic** | Motion detection → YOLO → InsightFace → instant Telegram photo alert → async VLM follow-up |
| **armed** | Same as basic + per-frame tracking for continuous monitoring |

### Detection Pipeline (basic/armed)

```
  USB Webcam (640x480)
       │
       ▼
  Motion detection (frame diff)
       │ motion detected
       ▼
  YOLOv8-nano (6 MB, ~50ms)
       │ person detected
       ▼
  InsightFace buffalo_l (300 MB)
       │
       ├── Known person → log arrival, NO alert
       │
       └── Unknown person
            ├── send_telegram_photo() → Philip's Telegram (<2 seconds)
            └── escalate_unknown_vlm() → async fire-and-forget
                 ├── llama3.2-vision:11b scene analysis
                 ├── send_telegram_text() → VLM description follow-up
                 └── Ingest to memory system
```

- Models loaded at startup unconditionally (~306 MB)
- 120-second `PERSON_ALERT_COOLDOWN` prevents alert spam
- InsightFace runs on CPU (no CUDA provider on this system)
- Mode switchable at runtime without restart: `POST /mode {"mode": "armed"}`
- Live stream: `https://cam.robothor.ai/webcam/` (Cloudflare Access protected)

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Service health check |
| `POST /mode` | Switch vision mode |
| `POST /look` | Capture + analyze snapshot |
| `POST /detect` | Run YOLO detection |
| `POST /identify` | Run face identification |
| `POST /enroll` | Enroll a face for recognition |

---

## CRM Stack

CRM data lives in native PostgreSQL tables (`crm_*`) in the `robothor_memory` database. The Bridge service provides REST proxy access and contact resolution.

```
  ┌───────────────────────────────────────────────────────────────┐
  │                  CRM (Native PostgreSQL)                      │
  │                                                               │
  │  crm_people         crm_companies        crm_notes           │
  │  crm_tasks          crm_conversations    crm_messages        │
  │                                                               │
  │  All in robothor_memory database                              │
  └───────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  Bridge Service :9100 (native Python, not Docker)             │
  │                                                               │
  │  - Contact resolution (cross-system identity via              │
  │    contact_identifiers table)                                 │
  │  - REST API for CRM data access (via crm_dal)                 │
  │  - Webhook endpoints                                          │
  │  - Data sync between CRM tables + memory system               │
  └───────────────────────────────────────────────────────────────┘
```

### Cross-System Identity

The `contact_identifiers` table maps every channel+identifier tuple to:
- CRM person ID (`person_id`)
- Memory system entity ID

This allows a single person to be recognized whether they email, call, text, or appear on camera.

---

## Memory System

Three-tier memory with structured facts and knowledge graph:

```
  ┌─────────────────────────────────────────────────────────────┐
  │                    MEMORY TIERS                              │
  │                                                             │
  │  Working Memory     Current session context window          │
  │                                                             │
  │  Short-term         PostgreSQL, 48-hour TTL, auto-decays   │
  │                                                             │
  │  Long-term          PostgreSQL + pgvector                   │
  │                     Permanent, importance-scored             │
  │                     ~945 facts, growing daily               │
  └─────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────┐
  │                 STRUCTURED LAYERS                            │
  │                                                             │
  │  memory_facts       Categorized facts with confidence,      │
  │                     lifecycle stage, conflict resolution,    │
  │                     1024-dim embeddings                      │
  │                                                             │
  │  memory_entities    Knowledge graph nodes                    │
  │  memory_relations   Knowledge graph edges                    │
  │                     (people, projects, technologies)         │
  │                                                             │
  │  Memory Blocks      5 named text blocks with size limits:   │
  │                     persona, user_profile, working_context,  │
  │                     operational_findings, contacts_summary   │
  └─────────────────────────────────────────────────────────────┘
```

### RAG Pipeline

```
  Query
    │
    ▼
  Qwen3-Embedding (0.6B) → 1024-dim vector
    │
    ▼
  pgvector similarity search → candidate facts
    │
    ▼
  Qwen3-Reranker (0.6B, F16) → cross-encoder scoring → top-K
    │
    ▼
  Qwen3-Next (80B, on-demand) → generated response with citations
```

### Ingestion Channels

Data enters through `POST /ingest` on the orchestrator (:9099):

| Channel | Source |
|---------|--------|
| `email` | Gmail sync |
| `calendar` | Google Calendar sync |
| `jira` | Jira sync |
| `google_meet` | Meet transcript sync |
| `discord` | Discord messages |
| `telegram` | Telegram messages |
| `camera` | Vision system events |
| `cli` | Direct CLI input |
| `api` | External API calls |

---

## Communications Layer

### Python Agent Engine

Single daemon handling agent orchestration, Telegram delivery, and cron scheduling.

| Component | Port | Purpose |
|-----------|------|---------|
| Engine | 18800 | Agent execution, Telegram bot, health API |
| Scheduler | — | APScheduler cron jobs from YAML manifests |
| Event Hooks | — | Redis Stream consumers (email, calendar triggers) |
| Tool Registry | — | 54 tools, direct DAL calls (no HTTP roundtrip) |

### Voice & SMS (Twilio)

| Service | Port | Number |
|---------|------|--------|
| Voice server | 8765 | +1 (413) 408-6025 |
| SMS webhook | 8766 | Same number |

Voice uses ElevenLabs (Daniel voice) for text-to-speech, Twilio ConversationRelay for call management.

---

## Tool Access Topology

Two runtime environments access the same underlying DAL:

```
  ┌─────────────────────┐              ┌─────────────────────┐
  │    Claude Code       │              │   Engine Agent      │
  │    (interactive)     │              │   (Kimi K2.5)      │
  └──────────┬──────────┘              └──────────┬──────────┘
             │                                     │
        stdio MCP                          direct DAL calls
             │                                     │
  ┌──────────┴──────────┐              ┌───────────┴─────────┐
  │   MCP Server         │              │  ToolRegistry       │
  │                      │              │  54 tools           │
  │  robothor-memory     │              │  (CRM, memory,      │
  │   44 tools           │              │   vision, web, I/O) │
  │   (memory + CRM +    │              └─────────────────────┘
  │    vision)            │
  └──────────────────────┘

  Tool names are IDENTICAL in both runtimes.
  Agent instructions work unchanged across Claude Code and Engine.
```

### MCP Servers

| Server | Runtime | Tools |
|--------|---------|-------|
| robothor-memory | Python (stdio) | search\_memory, store\_memory, get\_stats, get\_entity, memory\_block\_read/write/list, log\_interaction, look, who\_is\_here, enroll\_face, set\_vision\_mode, CRUD for people/companies/tasks/notes, search\_records, metadata, conversations, messages (44 tools total) |

---

## Cron Schedule

### System Crontab (Python, Layer 1 — mechanical, no AI)

| Schedule | Script | Purpose |
|----------|--------|---------|
| `*/5 * * * *` | calendar\_sync.py | Google Calendar → calendar-log.json |
| `*/5 * * * *` | email\_sync.py | Gmail → email-log.json |
| `*/30 6-22 * * 1-5` | jira\_sync.py | Jira → jira-log.json |
| `*/15 * * * *` | garmin\_sync.py | Garmin → garmin.db + garmin-health.md |
| `*/10 * * * *` | meet\_transcript\_sync.py | Google Drive → meet-transcripts.json |
| `*/10 * * * *` | continuous\_ingest.py | Tier 1: deduped ingestion → pgvector |
| `0 7,11,15,19 * * *` | periodic\_analysis.py | Tier 2: meeting prep, blocks, entities |
| `30 3 * * *` | intelligence\_pipeline.py | Tier 3: relationships, patterns, quality |
| `14,29,44,59 * * * *` | triage\_prep.py | Extract pending items → triage-inbox.json |
| `5,20,35,50 * * * *` | triage\_cleanup.py | Mark processed, update heartbeat |
| `*/10 6-23 * * *` | supervisor\_relay.py | Meeting alerts → Telegram |
| `0 3 * * *` | maintenance.sh | Memory maintenance (vacuum, decay) |
| `15 3 * * *` | crm\_consistency.py | Cross-system CRM checks |
| `0 4 * * *` | (find + delete) | Snapshot cleanup (>30 days) |
| `0 4 * * 0` | data\_archival.py | Sunday data archival |
| `30 4 * * *` | backup-ssd.sh | Daily LUKS SSD backup |
| `0 5 * * 0` | weekly\_review.py | Sunday deep synthesis |

### Engine Crons (Kimi K2.5, Layer 2 — LLM agent jobs via APScheduler)

| Schedule | Job | Purpose |
|----------|-----|---------|
| `0 6-22 * * *` | Email Classifier | Classify emails, route or escalate |
| `0 6-22/4 * * *` | Main Heartbeat | Surface escalations, audit logs |
| `*/10 * * * *` | Vision Monitor | Check motion events, alert on visitors |
| `30 6 * * *` | Morning Briefing | Daily briefing → Telegram |
| `0 21 * * *` | Evening Wind-Down | Tomorrow preview, open items → Telegram |
| `*/30 6-22 * * *` | Conversation Inbox Monitor | Check unread messages |

---

## Backup & Recovery

LUKS2-encrypted SanDisk SSD (1.8 TB) mounted at `/mnt/robothor-backup`.

| Field | Value |
|-------|-------|
| Schedule | Daily 4:30 AM |
| Encryption | LUKS2, keyfile unlock (slot 0) + passphrase fallback (slot 1) |
| Retention | 30 days for database dumps |

### What's Backed Up

| Category | Contents |
|----------|----------|
| Project directories | `clawd/`, `robothor/` (including `robothor/engine/`, `robothor/health/`) |
| Config directories | `.config/robothor/`, `.cloudflared/` |
| Credentials | `.bashrc`, `crm/.env` |
| Databases | 2x `pg_dump`: robothor\_memory, vaultwarden |
| Docker volumes | vaultwarden-data, uptime-kuma-data |
| System state | crontab export, Ollama model list, systemd service files |
| Verification | SHA256 manifest of all backed-up files |

---

## Folder Structure

```
robothor/                                 Project root (git repo)
├── CLAUDE.md                             Master project guide
├── INFRASTRUCTURE.md                     Hardware, networking, database
├── SERVICES.md                           Systemd services reference
├── pytest.ini                            Test configuration
├── run_tests.sh                          Layered test runner
│
├── docs/
│   ├── SYSTEM_ARCHITECTURE.md            This document
│   ├── CRON_MAP.md                       Unified cron timeline
│   ├── DATA_FLOW.md                      End-to-end data flow
│   └── TESTING.md                        Testing strategy & patterns
│
├── scripts/
│   ├── backup-ssd.sh                     Daily LUKS SSD backup
│   └── backup.log
│
├── crm/                                  CRM stack
│   ├── docker-compose.yml                Vaultwarden + Uptime Kuma + Kokoro TTS
│   ├── .env                              Docker secrets
│   ├── migrate_contacts.py               Contact migration tool
│   ├── contact_id_map.json               Migration mapping
│   ├── bridge/                           Bridge service (:9100)
│   │   ├── bridge_service.py             FastAPI app (webhooks, REST proxy)
│   │   ├── contact_resolver.py           Cross-system identity resolution
│   │   ├── crm_dal.py                    CRM data access layer (native SQL)
│   │   ├── config.py                     Bridge configuration
│   │   ├── requirements.txt
│   │   └── tests/
│   └── tests/                            CRM integration & regression tests
│       ├── test_phase0_prerequisites.sh
│       ├── test_phase1_services.sh
│       ├── test_phase3_memory_blocks.py
│       ├── test_phase4_mcp.sh
│       ├── test_email_pipeline.sh
│       └── test_regression.sh
│
├── brain/ → ~/clawd/                     Core workspace (symlink)
│   ├── SOUL.md                           Identity & personality
│   ├── AGENTS.md                         Agent config & startup
│   ├── ARCHITECTURE.md                   Three-layer architecture
│   ├── CRON_DESIGN.md                    Cron design principles
│   ├── HEARTBEAT.md                      Supervisor instructions
│   ├── WORKER.md                         Triage worker instructions
│   ├── IDENTITY.md                       Identity card
│   ├── MEMORY.md                         Curated long-term memory
│   ├── SECURITY.md                       Security policies
│   ├── TOOLS.md                          API keys, models, Cloudflare
│   ├── USER.md                           Philip's profile
│   ├── VISION.md                         Vision system reference
│   │
│   ├── memory/                           Runtime data (JSON logs)
│   │   ├── calendar-log.json             Calendar events
│   │   ├── email-log.json                Processed emails
│   │   ├── jira-log.json                 Jira tickets
│   │   ├── meet-transcripts.json         Google Meet transcripts
│   │   ├── meet-transcript-state.json    Transcript sync cursor
│   │   ├── contacts.json                 Contact profiles (legacy)
│   │   ├── tasks.json                    Task list
│   │   ├── worker-handoff.json           Escalations: worker → supervisor
│   │   ├── triage-inbox.json             Pending items for worker
│   │   ├── triage-status.md              Worker status for supervisor
│   │   ├── triage-prep-state.json        Prep script state
│   │   ├── heartbeat-state.json          Worker heartbeat timestamp
│   │   ├── relay-state.json              Relay cooldown state
│   │   ├── security-log.json             Security events
│   │   ├── sms-log.json                  SMS messages
│   │   ├── email-drafts.json             Draft emails
│   │   ├── email-tracking.json           Email tracking data
│   │   ├── health-status.json            System health snapshots
│   │   ├── garmin-health.md              Garmin health summary
│   │   ├── rag-quality-log.json          RAG quality metrics
│   │   ├── vision_mode.txt               Current vision mode
│   │   ├── weekly-review-*.md            Weekly synthesis reports
│   │   └── YYYY-MM-DD.md                Daily session logs
│   │
│   ├── memory_system/                    RAG & intelligence engine
│   │   ├── MEMORY_SYSTEM.md              Memory system docs
│   │   ├── INTELLIGENCE_PIPELINE.md      Pipeline docs
│   │   ├── mcp_server.py                 robothor-memory MCP server
│   │   ├── orchestrator.py               RAG orchestrator (FastAPI :9099)
│   │   ├── vision_service.py             Vision service (:8600)
│   │   ├── memory_service.py             Core memory CRUD
│   │   ├── rag.py                        RAG retrieval
│   │   ├── rag_query.py                  Query processing
│   │   ├── reranker.py                   Qwen3-Reranker integration
│   │   ├── ingestion.py                  Data ingestion core
│   │   ├── ingest_state.py               Dedup (watermarks, hashes)
│   │   ├── continuous_ingest.py          Tier 1 pipeline
│   │   ├── periodic_analysis.py          Tier 2 pipeline
│   │   ├── intelligence_pipeline.py      Tier 3 pipeline
│   │   ├── weekly_review.py              Sunday synthesis
│   │   ├── fact_extraction.py            LLM fact extraction
│   │   ├── conflict_resolution.py        Fact conflict handling
│   │   ├── entity_graph.py               Knowledge graph ops
│   │   ├── lifecycle.py                  Fact lifecycle management
│   │   ├── llm_client.py                 Ollama client wrapper
│   │   ├── contact_matching.py           Fuzzy name matching
│   │   ├── crm_fetcher.py               CRM data fetching via crm_dal
│   │   ├── web_search.py                 SearXNG integration
│   │   ├── transcript_watcher.py         Voice transcript processing
│   │   ├── transcript_sync.py            Transcript sync
│   │   ├── sync_sessions.py              Session sync
│   │   ├── maintenance.sh                Daily vacuum + decay
│   │   ├── conftest.py                   Test fixtures (gold standard)
│   │   ├── yolov8n.pt                    YOLO weights (6 MB)
│   │   └── test_*.py                     ~15 test files
│   │
│   ├── scripts/                          System crons (Layer 1)
│   │   ├── calendar_sync.py              */5 — Calendar sync
│   │   ├── email_sync.py                 */5 — Email sync
│   │   ├── jira_sync.py                  */30 — Jira sync
│   │   ├── meet_transcript_sync.py       */10 — Meet transcript sync
│   │   ├── triage_prep.py                :14,:29,:44,:59 — Prep for worker
│   │   ├── triage_cleanup.py             :05,:20,:35,:50 — Post-worker cleanup
│   │   ├── supervisor_relay.py           */10 — Telegram relay
│   │   ├── crm_consistency.py            Daily — CRM cross-checks
│   │   ├── data_archival.py              Sunday — Data archival
│   │   ├── system_health_check.py        Health monitoring
│   │   ├── cron_context.py               Shared cron utilities
│   │   └── email_processing.py           Email processing helpers
│   │
│   ├── voice-server/                     Twilio voice (:8765)
│   │   ├── server.py
│   │   └── server_gemini_live.py
│   │
│   ├── sms-server/                       Twilio SMS (:8766)
│   │   └── server.py
│   │
│   ├── robothor-status/                  Homepage (:3000)
│   │   └── server.js
│   │
│   ├── robothor-status-dashboard/        Status dashboard (:3001)
│   │   └── server.js
│   │
│   ├── dashboard/                        Ops dashboard (:3003)
│   │   └── server.js
│   │
│   ├── privacy-policy/                   Privacy page (:3002)
│   │   ├── server.js
│   │   └── index.html
│   │
│   ├── hooks/                            Event hooks
│   ├── canvas/                           Canvas UI
│   ├── welcome/                          Welcome page
│   └── gap-analysis/                     Architecture analysis
│
├── robothor/engine/                      Python Agent Engine
│   ├── daemon.py                         Main entry: Telegram + scheduler + hooks + health
│   ├── runner.py                         Core LLM conversation loop (litellm)
│   ├── tools.py                          54-tool registry with direct DAL calls
│   ├── telegram.py                       aiogram v3 Telegram bot
│   ├── scheduler.py                      APScheduler cron from YAML manifests
│   ├── hooks.py                          Redis Stream event-driven triggers
│   ├── tracking.py                       agent_runs + agent_run_steps DAL
│   └── tests/                            89 unit tests
│
├── robothor/health/                      Garmin health package (PostgreSQL)
│   ├── sync.py                           */15 — Garmin API → health_* tables
│   ├── summary.py                        2x daily — health_* → garmin-health.md
│   ├── dal.py                            Data access layer (upsert/query)
│   ├── migrate_sqlite.py                 One-time SQLite→PG migration
│   └── .garmin_tokens/                   OAuth credentials
│
├── templates/                             Bootstrap templates
│
└── tunnel/ → ~/.cloudflared/             Cloudflare tunnel
    ├── config.yml                        Tunnel ingress rules
    └── tunnel-token.txt                  Tunnel auth token
```

---

*Updated 2026-02-27. For questions, contact philip@ironsail.ai.*
