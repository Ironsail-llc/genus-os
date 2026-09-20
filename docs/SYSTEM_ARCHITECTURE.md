# Genus OS — System Architecture

> Technical reference for the Genus OS platform.
> Last updated: 2026-07-13
>
> Most service names, hardware details, schedules, and Cloudflare routes below
> describe one systemd appliance deployment. They are an operational snapshot,
> not defaults, availability measurements, or inherited security controls for
> every Genus OS installation. The Kubernetes release-candidate boundary is
> called out separately below.

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
9. [Task Lifecycle](#task-lifecycle--short-list--thread-pool)
10. [Self-Improvement Loop (Buddy)](#self-improvement-loop-buddy)
11. [Vision System](#vision-system)
12. [CRM Stack](#crm-stack)
13. [Memory System](#memory-system)
14. [Communications Layer](#communications-layer)
15. [Tool Access Topology](#tool-access-topology)
16. [Cron Schedule](#cron-schedule)
17. [Backup & Recovery](#backup--recovery)
18. [Folder Structure](#folder-structure)

---

## Executive Summary

The reference appliance is designed to operate an autonomous AI entity
continuously on dedicated hardware. It manages configured communications,
calendar, contacts, security monitoring, and knowledge workflows. Continuous
operation is an operating objective, not an availability claim.

**Core capabilities:**

- Continuously operated vision monitoring with face recognition and configured alerts
- Three-tier intelligence pipeline: ingest (10 min) → analysis (4x/day) → deep synthesis (daily)
- Unified CRM across email, Telegram, Google Chat, SMS, voice, and video meetings
- RAG-powered memory with structured facts, entity graph, and working memory blocks
- Autonomous triage: categorizes, handles routine items, escalates complex ones
- Voice calling and SMS via Twilio, Telegram delivery via Python Agent Engine

**Reference-appliance constraints:**

- Single-machine deployment (no cloud compute)
- All services managed by systemd (system-level, `Restart=always`)
- All external access via Cloudflare Tunnel (no open ports)
- LLM inference is local (Ollama) for embeddings/reranking/vision; remote (OpenRouter) for agent work

### Production release-candidate security boundary

The Helm deployment has a different boundary from the systemd appliance:

- The dashboard requires an Auth.js OIDC session and a successful Bridge token
  exchange. Existing accounts require an explicit issuer/subject binding;
  verified email alone does not auto-link or grant a privileged role.
- Bridge private routes verify signed issuer/audience/expiry/tenant/role/scope
  claims and apply route-specific scope and tenant checks.
- Vault secret **values** are write-only from every human surface. `GET
  /api/vault/get` serves a verified service token only — an owner/admin browser
  session is refused and audited, and the dashboard's bridge proxy
  (`app/src/app/api/bridge/[...path]`) will not forward the path at all. Humans
  set and rotate secrets with `robothor vault` on the appliance.
  `/api/vault/list` stays open to operators because it returns key names only.
- Bridge mutations under `/api/` either call `require_operator(request)` or are
  scope-gated by the middleware; `crm/bridge/tests/test_mutations_are_gated.py`
  enumerates the assembled app and fails on any route that is neither, so a new
  ungated mutation cannot ship unnoticed. Appliance-global acts (marketplace
  agent install/update/remove) are operator-gated and write an audit row.
- Engine non-probe HTTP routes and the IDE WebSocket independently verify
  signed, tenant-bound Engine scopes. Channel webhooks use their own
  route-specific HMAC. Empty roles do not imply privileged execution.
- Model-generated dashboards are sanitized, sandboxed, static HTML. Links,
  forms, controls, scripts, network calls, and mutation channels are rejected.
  The dashboard forwards verified identity to the Engine; only the Engine
  selects the model and holds provider credentials.
- Orchestrator and vision do not yet independently enforce the signed identity
  contract. They must remain private/NetworkPolicy-restricted services; neither
  is approved for direct public exposure. Vision is outside the current chart.
- Production/staging chart values split secret classes per workload and require
  default-deny NetworkPolicy with exact operator-supplied DB, cache, IdP,
  provider/payment, Kubernetes API, or controlled-proxy CIDRs. Broad internet
  CIDRs fail rendering.
- The canonical PostgreSQL chain contains 83 ordered, checksum-verified
  migrations. Legacy-memory tables are archived, and the legacy score-column
  cutover enforces a 30-day data gate plus full-row archive before dropping
  columns. Managed databases must provide `vector`, `uuid-ossp`, `citext`, and
  `pgcrypto`.
- Customer payment handling accepts provider tokens; Entity spend accepts
  provider-issued virtual-card references. Neither boundary accepts raw
  PAN/CVC/CVV, and neither is a PCI certification or a live payment adapter.
- Engine remains one replica because it owns scheduling and consumer state.
  Other replicas, PDBs, and readiness checks do not make the Engine highly
  available. The 99.9% availability, 15-minute RPO, and 60-minute RTO values are
  targets until failover and restore drills measure them.

Changes to this boundary are proposed and tested through a draft PR. Merge and
deployment are separate human-approved operations.

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
│  VPN:    Tailscale (your Tailscale tailnet)                       │
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

### Model Dispatch (`robothor/engine/llm_client.py`)

Every agent turn walks a fallback chain, and each model in it gets one in-place
retry before the chain advances. Three properties of that loop are worth
knowing when reading `agent_run_steps` or the journal:

- **A reasoning-only reply is not an empty one.** A thinking model that spends
  its budget before the answer starts returns blank `content` with
  `reasoning_content` / `reasoning_details` on the message. That is re-asked
  **once on the same model** with a smaller thinking budget and a nudge for the
  answer — an identical re-roll truncates identically — and logged as
  `reasoning_only`, distinct from a true provider `empty` (no reasoning, no
  tool call). `compaction.py` does the same rather than walking its chain down
  to the local tier, which is how an 81k-token context once compacted to a
  30-character summary.
- **The thinking budget is a share of the completion.** The running agent's
  `reasoning_effort` picks the share (low 25% / medium 50% / high 75% / max
  90%), capped so the answer always keeps `MIN_ANSWER_TOKENS`. A share, not a
  token count: every `supports_thinking` model in the fleet requests the same
  16,384 output, so absolute rungs all clamped to one number and three of the
  four manifest settings reached the wire identically. On that 16,384 the
  rungs are 4,096 / 8,192 / 12,288 — and `high` and `max` are **the same
  number**, because 75% is already the `MIN_ANSWER_TOKENS` cap. They separate
  only on a model with a larger `default_output_tokens`; today `max` buys
  nothing over `high`. `temperature` is forced
  to 1.0 only for the Anthropic family, which is the API that requires it.
- **Every attempt is recorded, not just the one that worked.** Each attempt
  writes its own `agent_run_steps` row with its own `duration_ms`; the failed
  ones carry the outcome (`empty`, `reasoning_only`, `reasoning_only_retry`,
  `timeout`, `error_<status>`) and the response's `finish_reason` and token
  counts in `error_message`. Before this the surviving row's duration silently
  covered every retry and every backoff, and a failed attempt left no row at
  all. `duration_ms` on an `llm_call` row means the **provider attempt** on both
  the streaming and non-streaming paths — never the token-counting prep before
  it — and the rows are written in a `finally`, so a spent credential or a
  run-deadline cancellation keeps its evidence (a cancelled in-flight attempt
  records `cancelled` and the cancellation still stands). One provider call is
  one row: a streamed reply that carries no answer advances the chain the way a
  non-streamed one does, rather than returning a blank turn and recording both
  a failure and a success for the same call. A reasoning-only reply the same
  model is re-asked about is written as `reasoning_only_retry`, so a query can
  tell a self-heal from a model that gave up.
- **An attempt row is telemetry, not a run error.** `record_llm_call` never
  sets `error_message`, so an `llm_call` row that has one is an attempt row
  (`llm_attempts.is_attempt_step`). They are excluded from the verifier's error
  count, the goal judge's `tool_errors`, Buddy's error evidence, the
  `total_llm_calls` / `request_count` turn counters, and from
  `models_attempted` — that column means *the models that actually served*, and
  the primary-model-dead detector decides "reached" by membership alone.
- **Unattended triggers get the batch timeout, and retries share it.** `cron`,
  `workflow`, `event` and `sub_agent` runs get `ROBOTHOR_LLM_TIMEOUT_BATCH`
  (300s) per model; interactive triggers (`telegram`, `webchat`, `slack`, and
  every other trigger by default) keep `ROBOTHOR_LLM_TIMEOUT` (120s), because
  there a human is waiting. An inbound-mail classification or a spawned
  sub-agent is as batch-shaped as a cron, and capping those at 120s was the
  bulk of the fleet's timeouts. Every attempt on a model — the in-place retry
  and the reasoning-only re-ask included — shares **one** allowance of that
  length, so one dispatch is bounded by
  `worst_case_dispatch_seconds(models, timeout)`. At the batch timeout that is
  305s per model, so a dispatch fits inside the 1800s
  `thread_pool.PENDING_EXPIRY_SECONDS` a sub-agent turn is expected to fit in
  **for chains of at most `MAX_CHAIN_MODELS_BUDGETED` (5) models** — the number
  the arithmetic is tested against. A longer chain is an operator's choice and
  is logged, not refused; the local tier's own 600s allowance is longer still,
  so a chain ending there needs the same check made by hand.
  Inside a workflow step, `workflow_budget.bound_call_timeout` clamps the same
  number again to what the workflow has left, and refuses to start a call the
  workflow cannot afford to finish. The two clamps compose in that order inside
  the attempt loop: the workflow's remaining budget can only ever lower the
  per-model allowance, never raise it, and a reasoning-only re-ask is a real
  provider call that has to clear both.

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
| Status page | robothor-status.service | 3000 | Node.js | ${INSTANCE_DOMAIN} homepage |
| Dashboard | robothor-status-dashboard.service | 3001 | Node.js | status.${INSTANCE_DOMAIN} |
| Ops dashboard | robothor-dashboard.service | 3003 | Node.js | ops.${INSTANCE_DOMAIN} |
| Privacy policy | robothor-privacy.service | 3002 | Node.js | privacy.${INSTANCE_DOMAIN} |
| CRM stack | robothor-crm.service | 3010, 8880 | Docker Compose | Uptime Kuma, Kokoro TTS |
| Bridge | robothor-bridge.service | 9100 | Python/FastAPI | Contact resolution, webhooks, REST proxy |
| Agent Engine | robothor-engine.service | 18800 | Python/FastAPI | Agent orchestration, Telegram, cron scheduler |
| Transcript watcher | robothor-transcript.service | — | Python | Voice transcript processing |
| Tunnel | cloudflared.service | — | Go binary | Cloudflare Tunnel (all external routing) |
| VPN | tailscaled.service | — | Go binary | Tailscale mesh (your Tailscale tailnet) |

---

## Network Edge — Cloudflare Tunnel

In the reference systemd appliance, externally routed traffic uses a single
Cloudflare Tunnel and host ports are not directly exposed. This is not the Helm
chart's ingress model and should not be copied as proof of application-layer
authorization.

### Externally reachable routes

| Subdomain | Port | Service | Required boundary |
|-----------|------|---------|-------------------|
| ${INSTANCE_DOMAIN} | 3000 | Status homepage | Deliberately public content only |
| status.${INSTANCE_DOMAIN} | 3001 | Status dashboard | Deliberately public, non-sensitive status only |
| dashboard.${INSTANCE_DOMAIN} | 3001 | Status dashboard (alias) | Same as status route |
| privacy.${INSTANCE_DOMAIN} | 3002 | Privacy policy | Deliberately public content only |
| voice.${INSTANCE_DOMAIN} | 8765 | Twilio voice webhooks | Provider-signature validation at service/edge |
| sms.${INSTANCE_DOMAIN} | 8766 | Twilio SMS webhooks | Provider-signature validation at service/edge |

Webhook reachability is not anonymous authorization. A deployment that cannot
verify the provider signature must not enable the route.

### Private operator/service routes

The reference appliance also uses Cloudflare Access. Application and network
requirements still apply:

| Subdomain | Port | Service | Application requirement |
|-----------|------|---------|-------------------------|
| cam.${INSTANCE_DOMAIN} | 8890 | Live webcam HLS stream | Private edge policy; biometric/privacy controls |
| ops.${INSTANCE_DOMAIN} | 3003 | Legacy ops dashboard | Private edge policy; do not expose sensitive views anonymously |
| bridge.${INSTANCE_DOMAIN} | 9100 | Bridge API | Signed scoped Bridge token in addition to the edge |
| engine.${INSTANCE_DOMAIN} | 18800 | Agent Engine API | Signed scoped Engine authority in addition to the edge |
| orchestrator.${INSTANCE_DOMAIN} | 9099 | RAG orchestrator API | No independent signed-auth boundary yet; keep private |
| vision.${INSTANCE_DOMAIN} | 8600 | Vision API | No independent signed-auth boundary yet; keep private |

### Network Topology

```
Internet → Cloudflare Edge → Tunnel (cloudflared) → localhost:<port>
                                                         │
                                              All camera ports bound
                                              to 127.0.0.1 only
```

Docker containers in this legacy appliance reach host services via
`172.17.0.1`. Its PostgreSQL/Redis bridge exposure, including Redis
`protected-mode off`, is instance-specific risk that requires host firewall and
trusted-container isolation; it is not a production chart recommendation.

---

## Data Layer

### PostgreSQL 16 + pgvector 0.6.0

Two databases on the same instance:

| Database | Owner | Purpose |
|----------|-------|---------|
| `robothor_memory` | `$PGUSER` | Facts, entities, contacts, memory blocks, ingestion state, CRM data, vault secrets |

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
| `workflow_approvals` | A workflow step waiting on a human verdict, and the verdict |
| `agent_questions` | A question an agent asked a person (`ask_user`, or a guardrail escalation) and the free-text answer. Separate from `workflow_approvals` because the workflow resume driver acts on every decided row it finds, and an answer is not a verdict |

### Canonical schema lifecycle

`robothor/migrations/manifest.txt` is the sole packaged migration order. The
runner takes an advisory lock, applies each file transactionally, records the
full migration ID and SHA-256 checksum, and refuses unknown history or drift.
The current chain has 83 entries. Migration 023 preserves legacy short/long
memory tables under explicit archive names. Migration 035 requires at least 30
days of replacement achievement data on populated upgrades and archives the
complete legacy rows before dropping superseded columns.

The chain is forward-only. Before an upgrade, pre-provision the required
PostgreSQL extensions (`vector`, `uuid-ossp`, `citext`, `pgcrypto`), take a
verified snapshot, run the full chain against a production clone, compare
material row counts, and rehearse restore.

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

- The 10-minute schedule is a freshness target; provider delay, backlog, and failures can increase it
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
3. **Engagement scoring** — Who is the owner interacting with most, and through which channels
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

Converts raw log data into prioritized actions, with an LLM gatekeeper controlling what reaches the owner.

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
  │  - Runs ON TELEGRAM (direct channel to the owner)               │
  │  - Reads *-status.md + worker-handoff.json                      │
  │  - Investigates before surfacing (no raw log dumps)             │
  │  - Sole gatekeeper: decides what's worth the owner's attention  │
  │  - Audits all logs for completeness                             │
  └─────────────────────────────────────────────────────────────────┘
```

**Design principles:**
- Main heartbeat never sends directly to Telegram via API — it runs as a Telegram agent session
- Python relay is the only script that calls the Telegram Bot API (meeting alerts only)
- Only 3 Engine jobs deliver to Telegram: Morning Briefing, Evening Wind-Down, SMS Status Check
- Calendar items older than 24h auto-expire in triage_prep

---

## Task Lifecycle — short list ↔ thread pool

Every task lives in one of two tiers, and flows between them automatically.

**Short list (in-run, ephemeral) — `todo_write`.**
Implemented in `robothor/engine/todolist.py`. An agent inside a single LLM loop calls `todo_write` with the steps it's about to execute, marks them `in_progress`/`completed` as it goes, and the runner injects a reminder every ~10 turns. The list evaporates when the run ends — it's per-run working memory, not persistent state.

**Long list (cross-beat, persistent) — the THREAD POOL.**
Tasks tagged `thread` in `crm_tasks`. Lives across heartbeats. Every main heartbeat, the warmup hook runs `auto_close_completed_threads()` → `plan_all_stalled()` → renders the pool (`robothor/engine/thread_pool.py:_thread_pool_context`). The forward planner (`robothor/engine/thread_planner.py`, Stage 4) reads each stalled thread's history and either writes a concrete `next_action` (heuristic matched) or `question_for_operator` (needs the operator). Autonomy decisions go through `robothor/engine/autonomy.py::classify_action` where objective vetoes beat numeric budgets.

**Connecting wire — auto-escalation (Stage 5).**
When a worker spawn carries `parent_task_id`, that ID is stored on `SpawnContext` (`robothor/engine/models.py`). At run end, `_escalate_unfinished_todos` (`robothor/engine/runner.py`) inspects `session.todo_list`. If any items are `pending` or `in_progress`:

1. Writes `Continue: <first unfinished item>` as the parent's `next_action` via `dal.set_next_action`
2. Adds `thread` tag if missing; seeds `objective` from the title if empty

Next heartbeat, the task appears in the pool with a concrete next step — closed loop.

```
create_task ──▶ worker run (todo_write)
                     │
                     ├─ all completed ──▶ resolve_task
                     │
                     └─ items pending ──▶ parent gets next_action + thread tag
                                              │
                                              ▼
                                        THREAD POOL
                                              │
                                 (next heartbeat) forward planner
                                              │
                                   ┌──────────┴──────────┐
                                   ▼                     ▼
                           main spawns worker      set_question →
                           with parent_task_id     operator decides
                                   │                     │
                                   └──────── loop ──────┘
```

**Flags.**
| Env var | Default | Controls |
|---|---|---|
| `ROBOTHOR_PLANNER_ENABLED` | `0` (off) | The Stage 4 forward planner. Off → stage-3 bare-flag stall2 behavior. |
| `ROBOTHOR_TODO_ESCALATE_ENABLED` | `1` (on) | Stage 5 escalation at run end. Only fires when both `parent_task_id` is set AND items remain unfinished. |

### Agent Goals — unified persistent objective per agent

The system has **one goal per agent**, persistent across runs, editable at runtime. It absorbs three previously-separate concepts: the operator's session objective, the agent's manifest metric targets, and the typed-evidence completion guard. The same goal task drives prompt injection, the buddy → auto-researcher → auto-agent self-improvement loop, the thread pool / forward planner, and Stage 5 todo escalation.

**Storage.** Each agent has a single `crm_tasks` row tagged `[session_goal, agent:<agent_id>, thread]`. The full payload lives in the `session_goal_meta` JSONB column:

```
{
  "objective":         "<operator-set>",
  "success_criteria":  ["…", "…"],
  "metric_targets":    [{id, category, metric, target, weight, window_days, extras}, …],
  "evidence":          [{kind, summary, reference, recorded_at, valid}, …],
  "completion_note":   "<text>",
  "alignment_target":  ">=0.7"        // buddy-judged session_goal_alignment
}
```

Migrations: `065_session_goal_meta.sql` (column), `066_session_goal_meta_v2.sql` (v2 shape doc). The instance's `brain/GOAL.md` is a denormalized read-cache regenerated on every mutation; hand-edits are advisory only.

**Manifest goals are SEED, not source of truth.** `docs/agents/<agent>.yaml` `goals:` blocks supply the initial `metric_targets` when an agent's goal task is first created (via `scripts/seed_agent_goals.py` or lazily on first read). After seeding, the unified task is canonical and edits go through the CLI/Telegram/tools — manifest changes are advisory.

**Composition.** `robothor/engine/goals.py::compose_goals(agent_id, manifest, tenant_id)` returns the merged `GoalSpec` list every consumer reads from:
- The unified task's `metric_targets` (if any), else falls back to the manifest's `goals:` block.
- A synthetic `session-goal-alignment` GoalSpec (metric `session_goal_alignment_score`, target from the task's `alignment_target`, weight 5.0) when an objective is set.
- A synthetic `session-goal-progress` GoalSpec (validated_evidence / criteria_count, target `>=1.0`) when criteria exist.

Both synthetic specs carry `GoalSpec.synthetic = True`. They exist in no manifest, so **breach accounting skips them unless the agent opts in** — `detect_goal_breach(..., include_synthetic=...)`, driven by `session_goals_enforced(manifest)`, which reads:

```yaml
session_goals:
  enforce: true
```

They are still *scored* (the metrics behind them are real), but the nightly review reports `categories.breached_manifest` and `categories.breached_synthetic` separately so a runtime-injected goal is never read as a declared contract.

`buddy.py`, `buddy_critic.py::aggregate_findings`, the nightly self-improvement sweep, and the audit CLI all switched to `compose_goals` — the manifest-only path is gone.

**Buddy alignment dimension.** When `build_evidence` finds an active goal for the run's agent, it includes `objective` + `success_criteria` in the LLM review prompt. The LLM rates `session_goal_alignment` 0.0-1.0 alongside its primary dimension. `persist_review` writes a second `agent_reviews` row with `categories.dimension = 'session_goal_alignment'` and `rating` mapped 1-5 from the alignment score. `_get_session_goal_alignment_score` reverses the map for `compute_goal_metrics`.

**Self-improvement loop.** The synthetic alignment goal flows through the existing pipeline:
- `detect_goal_breach` flags it when alignment drops below target.
- `aggregate_findings` opens a CRM task tagged `[nightwatch, self-improve, <agent>, session_goal_alignment]`.
- `auto-researcher` can target `session_goal_alignment_score` and `session_goal_progress` without `operator_override` — they ARE the operator's mandate (whitelist in `experiment.py`).
- `auto-agent` ships PRs against the breach.

**Per-agent prompt injection.** `robothor/engine/warmup.py` adds an `agent_goal` warmup phase that calls `session_goal::build_agent_goal_context(tenant_id, agent_id)`. Every agent sees its own goal block — objective, criteria, metric_targets, current grades, alignment score. Recorded as `warmup_section:agent_goal` in `agent_run_steps` for telemetry. No more owner-only scoping; the v2 model gives every agent its own goal.

**Real completion guard.** `complete_goal` refuses to mark the task DONE unless evidence includes:
- ≥1 valid `test_run` evidence — reference matching `pytest:(passed|failed):N` or a UUID, AND
- ≥1 valid `commit` evidence — reference is a 7+ char SHA validated via `git cat-file -e`.

Other kinds: `ci_run` (https URL) and `note` (free-form, never satisfies completion).

**Composition with the thread pool.** Because goal tasks carry the `thread` tag automatically, they appear in the thread pool (Stage 1), get planner next-actions written (Stage 4), participate in autonomy budgeting, and Stage 5 todo escalation already wires unfinished worker todos onto the goal task whenever a child run is spawned with `parent_task_id` pointing at it.

**Surfaces.**
- CLI: `robothor goal --agent <id> {set, status, evidence, complete, edit-objective, add-criterion, set-target, remove-target}`. Workspace resolves via `ROBOTHOR_WORKSPACE`.
- Telegram: `/goal {set, evidence <kind>, evidence-test pytest:passed:N, evidence-commit HEAD, edit-objective, add-criterion, set-target, done}`. `/goals` (plural) lists fleet benchmark grades.
- Tools: `create_goal`, `get_goal`, `update_goal` — `update_goal` accepts `edit_op ∈ {objective, criterion, metric_target}` plus the existing evidence/completion paths.

**Seeding.** `scripts/seed_agent_goals.py` walks `docs/agents/*.yaml` and ensures every real agent (skipping `_defaults.yaml`, `schema.yaml`, `corrective-actions.yaml`) has a goal task. Idempotent — agents that already have a task are left alone.

**Activation:**
```
sudo systemctl edit robothor-engine
  [Service]
  Environment="ROBOTHOR_PLANNER_ENABLED=1"
sudo systemctl restart robothor-engine
```
Kill switch: unset the env var, restart.

**Verification:**
```
curl localhost:18800/api/analytics/threads | jq
  # Watch planner_override_rate > 0.2, questions_answered_within_24h stable or rising
```

---

## Self-Improvement Loop (Buddy)

Rebuilt 2026-04-19 from a gamification scoreboard into an active reviewer + grader + guardrail triad. The core loop is: **observe real behaviour → rate it → flag what's broken → let auto-agent fix it → verify the fix held**. No free-form LLM commentary on heartbeats, ever.

### Scoring — `robothor/engine/goals.py`

Every agent declares a `goals:` block in its manifest (see `docs/agents/GOAL_TAXONOMY.md`). `compute_achievement_score(agent_id)` returns a weighted 0.0-1.0 score over the agent's goals, scaled to 0-100 and persisted to `agent_buddy_stats.achievement_score` by `BuddyEngine.refresh_daily()`. Legacy RPG columns (xp, level, debugging_score, patience_score, chaos_score, wisdom_score, …) were removed — migration 034 added `achievement_score`; migration 035 drops the legacy columns after a 30-day soak.

**A grade requires measurement.** A goal whose metric has no data this window is neither satisfaction nor breach, so it leaves the weighted denominator — which used to mean an agent satisfying 2 of its 7 goals reported a flawless 5/5, indistinguishable from an agent measured end to end. `compute_achievement_score` therefore also returns `coverage` (measured goal weight ÷ declared goal weight), `measured_goals` / `unmeasured_goals`, and `partial_score`. Below `MIN_MEASUREMENT_COVERAGE` (0.5) **no rating is emitted at all**: `score` and `rating` are `None`, `rating_reason` is `"insufficient measurement"`, and `partial_score` still reports what the measured slice scored so nothing is hidden.

`None` is carried all the way down — it is never coerced to a number:

| Surface | Unmeasured value |
|---------|------------------|
| `agent_reviews.rating` | `NULL` (migration 102 dropped the NOT NULL; the `BETWEEN 1 AND 5` CHECK still binds every non-NULL rating) |
| `AgentScore.achievement_score` / `.rating` | `None`, with `.measured == False` |
| `agent_buddy_stats.achievement_score` | `NULL` — every reader already filters `IS NOT NULL` |
| `FleetStatus.fleet_achievement_score` | mean over **measured** agents only, `None` when nothing was measured; `agents_measured` / `agents_total` carry the coverage |
| `/api/buddy/stats`, `/stats`, `/buddy` | `null` / `n/a (unmeasured)` — never `0/100` |

The nightly review (`run_nightly_auto_review`) opens every feedback body with `Measurement coverage: N of M goals measured`. It used to write `rating=achievement["rating"] or 3`, converting "we measured nothing" into a mid-range pass — on 2026-08-21 that produced 20 of 20 rows at 3/5 with `categories.score = null`.

### Review — `robothor/engine/buddy_critic.py`

The `buddy` agent (an instance manifest, `docs/agents/buddy.yaml`, cron `0 6-22 * * *`) runs two passes:

- **Hourly review pass** — for each agent with goals, sample up to 2 recent top-level runs biased toward failures / error steps / long durations and not already reviewed. Build a structured `Evidence` dict from `agent_runs` + `agent_run_steps`. Sonnet 4.6 phrases a rating (1-5) + dimension + `specific_issue` (≤ 80 chars referencing concrete evidence) + `suggested_action` (≤ 120 chars). Persist to `agent_reviews` with `reviewer_type='buddy'`.
- **6-hourly aggregation pass** — run `detect_goal_breach` per agent. For breaches with `priority_score ≥ 3.0` *and* a non-null current metric value, build a `Finding`: 3 representative reviews, corrective-action template from `docs/agents/corrective-actions.yaml`, live baseline metric. Create one `crm_tasks` row per finding tagged `nightwatch+self-improve+<agent>+<metric>` assigned to `auto-agent`. Dedups against open tasks for the same (agent, metric). The task body embeds a machine-readable `<!-- buddy-baseline: {...} -->` marker the grader parses later.

The LLM receives pre-computed evidence and is used only to phrase the finding,
which reduces but does not eliminate hallucination risk. Persisted evidence and
post-change metrics remain the authority.

### Verify — `robothor/engine/buddy_grader.py`

The `buddy-grader` agent (an instance manifest, `docs/agents/buddy-grader.yaml`, cron `7 * * * *`) closes the loop:

1. For every DONE self-improve task older than 48 hours with no verification tag yet, parse the baseline marker and re-run `compute_goal_metrics` for that metric.
2. Metric satisfies target → tag `verified_resolved` + resolution note.
3. Metric still breached → tag `verify_failed` + increment `escalation:N`, transition back to IN_PROGRESS. At `escalation:2` the task re-routes to `auto-researcher`; at `escalation:3` it's tagged `requires_human=true` and auto-escalation stops. This terminal state is mandatory — endless churn is worse than a known-open issue.
4. Separately, 7 days after `verified_resolved`, re-check the metric and tag `held_7d=true|false`. That's the data source for the weekly guardrail.

Env flag `ROBOTHOR_BUDDY_GRADER_DRYRUN=1` computes verdicts without writing, for operator-driven simulation.

### Guardrail — `robothor/engine/buddy_auditor.py`

The `buddy-auditor` agent (an instance manifest, `docs/agents/buddy-auditor.yaml`, cron `0 7 * * 1`) is the falsifiability clause. Weekly, it reads the `held_7d=true|false` tag distribution over the last 14 days. If **under 30%** of fixes held for 7 days (min 5 samples), it pauses Buddy's cron by editing `docs/agents/buddy.yaml` and sends a critical alert to `main`. Re-enabling is a deliberate human decision.

Piggybacked on the same weekly run: the instance's review-quality sentinel (`brain/scripts/buddy_review_quality_sentinel.py`) flags filler output if ≥ 20% of recent Buddy reviews fail a concrete-evidence heuristic.

### Observability

- `GET /api/buddy/ratings` — per-agent latest achievement + 7-day trend.
- `GET /api/buddy/reviews` — recent Buddy reviews, paginated.
- `GET /api/buddy/findings` — open/in-progress/verifying/resolved/persistent/requires_human buckets.
- `GET /api/buddy/verifications` — verified tasks with baseline → current → held_7d for the auditor.
- `brain/journals/buddy/YYYY-MM-DD.jsonl` (instance state) — append-only audit trail of every review, finding, verification, hold-check, and audit.

### What was deleted

`buddy_watch.py` (parallel LLM cron), `_maybe_append_buddy_reflection` in `delivery.py` (heartbeat appendix), `_buddy_status_context` warmup hook, `flag_underperformers` + escalation mechanics in `buddy.py`, XP/level/streak gamification (constants, LevelInfo, DailyStats dataclasses), `improvement-analyst` agent + workflow (subsumed by `buddy`'s aggregation pass). The legacy `nightwatch` workflow is retired and its definition removed.

### Run claim verification — `robothor/engine/run_verification.py`

The judge above rates *prose*, and prose judges are near-chance at catching an
agent that asserts work it never did — they anchor on confident language. This
module does the independent check: at the end of every run `_finish_run` calls
it (before `_persist_run`, so the verdict reaches `_assess_outcome` and the
persisted row), and it compares the run's **claims** against the tools that
actually **succeeded** in its own trace.

- **Pure** — `extract_claims(text)` + `match_claims_to_trace(claims, steps)`, no DB/LLM/clock, so real incidents replay as unit tests.
- **Claim classes** — `sent_email`, `sent_message`, `record_update`, `crm_write`, `calendar_event`, `file_written`, `scheduled`, `task_completed`, `payment`. Each names the tool families that can satisfy it. `payment` names none: no payment integration exists in this system, so the class is unsupported by construction.
- **`record_update` is the sharp one** — "filed / confirmed / tracked / noted / logged / updated / marked done" is a claim about a RECORD, satisfied only by a durable write (CRM, memory, calendar, schedule, or a file outside a temp dir). A note to `/tmp` is not a record.
- **Deferred tools** — RIP-16 routes most tools through the `tool_call` meta-tool, so `agent_run_steps.tool_name` is literally `'tool_call'` and the real name is at `tool_input['name']` (this is why `gws_gmail_send` shows 0 calls in per-tool analytics). `resolve_tool_name` unwraps that, including nesting.
- **Abstention is never punished** — negation and abstention windows (shared with `completion_contract.py`) drop "I could not send the email", and quoted / blockquoted / fenced spans are masked so a claim the agent *reports* is not a claim it *makes*.
- **Verdicts** — `no_claims` | `verified` | `unverified_claims` | `failed_verification`, persisted to `agent_runs.verified_status` + `agent_runs.verification` (jsonb) by migration 100, which also creates the per-claim evidence ledger `agent_run_evidence` (`run_id`, `step_id`, `kind`, `reference`, `verified`, `detail`, `created_at`; tenant scope inherited via `run_id`).
- **Flag** — `ROBOTHOR_RUN_VERIFICATION_ENABLED` / `_MODE` on the standard off→observe→alert→enforce ladder, shipping at **observe**: record only, no delivery gating, no task resolution, no change to `outcome_assessment`. See `docs/runbooks/GUARDRAIL_FLIPS.md`.

---

## Vision System

When operated continuously, the computer-vision service supports four modes:

| Mode | Behavior |
|------|----------|
| **disarmed** | Camera streams but no processing |
| **basic** | Motion detection → YOLO → InsightFace → instant Telegram photo alert → async VLM follow-up |
| **armed** | Same as basic + per-frame tracking for continuous monitoring |
| **disabled** | Service deliberately off (e.g. thermal constraints) — no camera analysis; persists across restarts until re-enabled |

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
            ├── send_telegram_photo() → owner's Telegram (<2 seconds)
            └── escalate_unknown_vlm() → async fire-and-forget
                 ├── llama3.2-vision:11b scene analysis
                 ├── send_telegram_text() → VLM description follow-up
                 └── Ingest to memory system
```

- Models loaded at startup unconditionally (~306 MB)
- 120-second `PERSON_ALERT_COOLDOWN` prevents alert spam (enforced in both basic
  and armed modes)
- Repeat sightings of the same unknown face are deduplicated by embedding
  similarity — no new `unknown_NNN` id or alert per frame
- At most one VLM follow-up in flight at a time (60s request timeout); snapshots
  are written only when an alert actually fires
- InsightFace runs on CPU (no CUDA provider on this system)
- Mode switchable at runtime without restart: `POST /mode {"mode": "armed"}`
- Live stream: `https://cam.${INSTANCE_DOMAIN}/webcam/` (Cloudflare Access protected)

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

#### Unified Identity Context

`robothor/identity/` is the platform seam every channel resolves an
interactive caller through, so the rest of the system reasons about one
identity shape instead of one per channel:

- **`crm_people` is the canonical identity** — one row per human. Every
  other identity table is a *credential/channel binding* pointing at a
  person: `user_accounts` (SSO/webchat), `tenant_users` (Telegram),
  `face_identities` (vision, migration 089), and `contact_identifiers`
  (all channels, plus the bridge into the memory graph). One human, one
  person, many bindings.
- **`user_channel_identities` (migration 118) is the generic binding** —
  `(tenant, channel, native_id) -> user_id` plus the granted `role`, who
  granted it and when it was revoked. Telegram still resolves through
  `tenant_users` (an approval writes both rows); Slack and every plugin
  channel resolve through this one. It is deliberately **not**
  `contact_identifiers`: that is a rolodex populated by ingestion, and
  conflating "we know this person" with "this person may drive the agent"
  would turn every parsed mailing list into an authorization.
- **`resolve_identity(channel, identifier, tenant_id) -> IdentityContext`**
  resolves any channel-native id (webchat account UUID, Telegram user id,
  a recognized face label, a paired Slack user) down to a common shape —
  `role`, `person_id`, `verified`, etc. — used uniformly by prompt assembly,
  permissions, and audit. A channel with no dedicated resolver is no longer a
  dead end: `_resolve_generic` answers out of `user_channel_identities`, so a
  channel earns resolution by having rows rather than by shipping code.
- **The `--- CURRENT USER ---` prompt block** (`IdentityContext.prompt_block`)
  is injected on the first turn of every interactive run (and re-injected
  on later turns in a lightweight form) so the agent always knows who it's
  talking to, enriched with CRM affiliation and memory-graph relationships
  when a `person_id` is resolvable.
- **"Own data + shared" scoping** — non-privileged identities (role not in
  `{owner, admin, service}`) draw only on rows linked to their own
  `person_id`, plus org-general (`person_id IS NULL`) rows; owner/admin/
  service and system/cron callers see everything in-tenant unchanged. See
  `robothor/identity/scope.py` and `docs/runbooks/IDENTITY_ROLLOUT.md` for
  the flags, rollout order, and CLI.
- **Inbound access policy** — `pairing | allowlist | open`, per channel, is
  what decides whether an *unresolved* sender gets a run at all. One gate
  (`robothor/engine/channels/access.py`) for every channel, and one invariant:
  a channel message may never approve a pairing. See
  [Channel access](channels/access.md).

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

#### Schedule reconciliation

The manifests on disk are the source of truth; the APScheduler job set is a
derivative, and reconcile is what makes the two agree. `CronScheduler.reconcile`
runs from the watchdog every five minutes and on demand from
`POST /api/admin/scheduler/reconcile`, and both **add, replace and prune**:

| Outcome | When |
|---------|------|
| `added` | A manifest declares a job the scheduler does not hold — a new agent, or one whose `schedule.enabled` just went back to `true` |
| `replaced` | The job exists with a different trigger or misfire grace. Compared on `repr(trigger)`, because `CronTrigger.__eq__` is identity and `str(trigger)` omits the timezone — so a timezone-only edit is invisible to either obvious comparison |
| `refreshed` | The trigger is unchanged but the `agent_schedules` row is not: a model, delivery or session-target edit. Those columns are what the fleet view, `routers/agents.py` and `gen_cron_map.py` read, so skipping the write left the appliance's own state table disagreeing with the manifest until a restart. Its own list because nothing about the running job moved |
| `pruned` | The manifest is gone, or `schedule.enabled: false`. The DB row survives a disable so the fleet view can show "off" rather than "deleted" |
| `blocked` | Nothing happened. See below |

`robothor/engine/schedule_reconcile.py` owns the derivation ("which jobs does
this manifest ask for") and `scheduler.start()` consumes the same function, so
boot and reconcile cannot disagree about a namespace — which is how
`main:worker` came to be pruned five minutes after every restart. Job ids under
`_SYSTEM_JOB_PREFIXES` (`workflow:`, `memory:`, `plugin:`) are never touched: a
manifest cannot know about them, so reconcile must not judge them.

**The dirty-scan interlock.** A scan that could not read every manifest is
authority for nothing: reconcile adds nothing, replaces nothing, prunes nothing,
and reports `blocked` as `{agent-id: ErrorType}` (or `{"*": "manifest directory
unreadable"}`). On 2026-08-23 a YAML typo made one manifest unparseable, the
loader dropped it, reconcile could not tell "broken" from "deleted", and it
deleted the primary agent's heartbeat and worker schedules; the operator got
silence for 3h48m. Pruning is one-way and `agent_schedules` has no tombstone, so
the refusal is not negotiable — and adding from a partial view is refused for
the milder version of the same reason: a trigger derived from an incomplete
fleet is one nobody can account for.

A write that does not reconcile is a write that changes nothing, so every
manifest writer calls it: `POST`/`PATCH`/`DELETE /api/agent-manifests`,
`/api/installed-agents` install/update/remove, and `POST /api/setup/agent`.

#### Workflow budgets and step visibility

A workflow's `timeout_seconds` is one wall-clock budget shared by all of its
steps, and an agent step spends it walking that agent's model chain — primary,
one in-place transient retry, then each fallback, each leg with its own per-call
allowance (`LLM_REQUEST_TIMEOUT_BATCH` for cloud models on a workflow or cron
trigger, `LLM_REQUEST_TIMEOUT_OLLAMA` for the local tail). **No step may be
allowed more wall-clock than the workflow that contains it.** `email-pipeline`
carried a 900 s budget while its classify step's four-model chain was allowed
`300 + 300 + 300 + 600` plus one 300 s retry — 1,800 s — so the run could only
ever finish while the primary answered first try; when the primary began
returning empty completions on 2026-09-11 every run died at exactly 900 s.
`robothor/engine/workflow_budget.py` enforces this at both ends.
`load_workflows` logs a `Config validation [workflow:…]` warning naming the
step, its computed worst case and the budget — the same ladder as the
agent-manifest check `_check_stall_budget_vs_llm_timeout`, which validates the
identical inversion for stall budgets — and `scripts/validate_agents.py --ci`
runs the same check with `strict=True`, so an inversion it can resolve fails
the PR instead of scrolling past. Both surfaces report **how much they
checked**: an unresolved chain must never be mistaken for a clean one. Because
every agent manifest is gitignored, the CLI falls back to
`llm_budgets.REFERENCE_CHAIN` — the four-model shape the platform ships — so
the *tracked* `docs/workflows/*.yaml` budgets are genuinely checked on a clean
checkout, and a green `validate-agents` job is evidence rather than an absence
of it. Checking zero agent steps is itself reported as a failure. The arithmetic reads its constants from `robothor/engine/llm_budgets.py`,
the same leaf `llm_client` spends them out of, so prediction and runtime cannot
drift; that leaf is also why the check needs no provider SDK to run.

At runtime the workflow publishes its deadline so each LLM call is clamped to
what is actually left. The chain walk refuses to *start* a model the budget can
no longer afford, raising `WorkflowDeadlineError` — which names the workflow,
the step and the refused model, and which `runner.execute` classifies as a
**cancellation, not a timeout**: the agent's own clock never fired, so counting
it in the timeout rate would be the same corruption `GENUINE_TIMEOUT_SQL`
exists to prevent. The runner writes its row and re-raises, because only the
workflow engine knows which step to mark — and so does `ToolRegistry.execute`,
since `spawn_agent` runs its child `runner.execute` inline in the parent's task
and therefore inside the same deadline scope; left to the registry's broad
`except TimeoutError` it became "Tool 'spawn_agent' timed out after 120s", a
duration that never elapsed.

Being a cancellation has one consequence worth stating. `cancelled` is in
`RESUMABLE_STATUSES`, so without a second rule a restart would resume an agent
run its workflow deliberately abandoned — outside any deadline, with no
workflow left to report to, and spending the budget that was already exhausted.
`resume.resumable()` therefore drops runs whose reason starts with
`WORKFLOW_BUDGET_CANCEL_PREFIX`: the one `cancelled` that is a decision rather
than a casualty.

The deadline is one budget for the whole run, **not a per-step allowance**: it
stops a step outspending its workflow, and does not stop a first step leaving
the second one nothing. Dividing it would need a per-step budget in the YAML,
which no workflow declares today.

A step's `workflow_run_steps` row is written when the step is **dispatched**,
as `running` with its `started_at`, and updated in place when it finishes. It
used to be written only on completion, so a step that never returned left no row
at all: timed-out runs read `steps 0/2` with an empty step table, and "the
workflow never started" was indistinguishable from "step 1 has been running for
fifteen minutes". Rows still `running` when the run ends are closed out as
`timeout` (migration 119) rather than left immortal, and the run's own
`error_message` names the step — or, for a parallel fan-out, the steps — that
were in flight. `WorkflowStepStatus` is held against that CHECK constraint by
`test_schema_drift.py`, the same guard its three sibling enums already had.

#### Engine admin routes

Everything under `/api/admin` requires the `engine:control` scope
(`robothor/engine/auth.py::_CONTROL_PATHS`), reads included, and a route added
under that prefix inherits the requirement rather than having to remember it.

| Route | Module | Purpose |
|-------|--------|---------|
| `GET /api/admin/providers`, `POST .../{id}/test`, `GET /api/admin/models`, `POST /api/admin/secrets/reload`, `POST /api/admin/defaults/reload` | `admin_providers.py` | Credentials, the model catalogue, a real test completion |
| `POST /api/admin/scheduler/reconcile` | `admin_scheduler.py` | Re-derive the job set; answers `{added, replaced, refreshed, pruned, blocked, clean}`, or 503 when this process holds no scheduler |
| `GET /api/admin/tools` | `admin_scheduler.py` | The registered tool names a manifest may name. The bridge's manifest validator reads this rather than importing `ToolRegistry`, because only the engine process knows what its plugins contributed |
| `POST /api/admin/approvals/escalation/{id}` | `admin_approvals.py` | Settle a pending permission escalation. The request is an `asyncio.Event` in **this** process, so the bridge proxies here rather than writing a row; 404 when the prompt is gone (timed out, already decided, previous process), 503 when this engine holds no manager |

Engine alerts (`robothor/engine/alerts.py`) route by severity: `critical`
pages Telegram immediately; `warning`/`info` become `alert_digest`
notification rows in `crm_agent_notifications` addressed to the operator-facing
agent. `robothor/engine/warmup.py` is the reader: both the heartbeat preamble
and the operator's first interactive turn render an `UNREAD ALERTS (N)` section
and acknowledge only the rows whose text survived into the delivered preamble
(see `docs/runbooks/PAGING.md`).

#### Warmup: live host state

`build_warmth_preamble` (`robothor/engine/warmup.py`) runs its sections —
unread alerts, history, memory blocks, context files, peers, context hooks,
breadcrumbs, preferences, agent hooks, agent goal, goal recall, active intents
— and then the registered context hooks: `_date_context`, `_travel_status`,
`_weather_context`, `_git_status_context`, `_thread_pool_context` and
`host_state_context`.

`robothor/engine/host_state.py` is the only section that probes the running
host. It emits three facts in words, headed "LIVE ENGINE STATE … as of now":

| Fact | Source |
|------|--------|
| Engine uptime, as an **age** | `systemctl show -p ActiveEnterTimestamp -p ActiveState --timestamp=utc <unit>` where systemd is booted (retried without `--timestamp=utc` on systemd < 247). Never `NRestarts` — systemd zeroes that on a manual or deploy restart. An age is rendered only when `ActiveState` is `active`; any other state reads "the engine service is NOT running … systemd reports the unit `<state>`", because a failed unit keeps its last start's timestamp. |
| Platform version | `robothor.__version__` |
| Last-24h model reach | One aggregate over `agent_runs.model_used`, tenant-scoped, via `crm.dal.get_model_reach_24h` |

It exists because an agent had no other source of truth about its own host.
On 2026-09-13 the operator-facing agent reported that the engine "hasn't been
restarted since Sep 3" and that the fleet was "mostly running on fallback
models" — both false as spoken (the engine had restarted that morning; 98.8% of
the day's runs reached the primary). Both came from undated `memory_facts` rows
that were true when written and were recalled as present tense. The section says
it is live so the model has a reason to prefer it over such a recollection.

**It renders on both preambles.** `build_warmth_preamble` reaches it through the
registered agent context hook; `build_interactive_preamble` calls
`host_state_section(agent_id, agent_config)` directly, via
`_interactive_supervisor_sections`. That is not belt-and-braces: agent context
hooks run from the cron builder alone, and the operator was in *chat* when the
stale fact was asserted, so a section only on the cron path would have missed
the channel the incident happened on.

**The manifest has to reach it.** `runner.execute` threads `agent_config` into
`build_interactive_preamble`, because the reach sentence needs the configured
primary. Without it the section could not tell "this agent has no primary" from
"nobody told me which one" — and it asserted the former, so every chat turn had
main reporting that it had no configured primary. It has one; manifests carry
`model.primary`. A caller that genuinely has only an id now gets the neutral
"the busiest model was …" wording instead of a claim about configuration.

**It is a reason to warm, not a passenger.** `wants_cron_warmup` (the predicate
`runner.execute` uses to decide `warmup_kind`) is true when the manifest names
warmup memory blocks, context files or peer agents — *or* when the agent gets
host state. Without that, a heartbeat agent with no `warmup:` block built no
preamble at all and the targeting bought nothing.

**Degradation is uptime's whole design.** On a systemd host a failed, wedged or
empty probe renders `Engine uptime: unknown` — never this process's own clock.
(`systemctl show` for a unit that does not exist exits 0 with empty output, so
`ActiveState` is read alongside the timestamp; the `/proc` process clock is used
only where there is no systemd to ask.) The model-reach line degrades the same
way, and runs that reached no model are counted and named as such rather than
appearing as a model called `none`.

Bounded and optional: at most two `systemctl` calls at 0.5 s each, plus one
query with a 1 s `statement_timeout`, memoised 60 s per (agent, configured
primary), rendered only for the operator-facing agent
(`OPERATOR_INBOX_AGENT_ID`) and agents carrying a `heartbeat:` block — workers
get nothing, and make no calls.

**One off switch.** `host_state.set_host_state_enabled(False)` disables every
path — the hook, the interactive builder, and `wants_cron_warmup`'s reason to
warm — because all three ask `wants_host_state`. Dropping the
`register_agent_context_hook` call, which was the documented opt-out when this
was cron-only, now disables the scheduled path alone. The unread-alert and
memory sections are untouched by any of it.

#### Delivery status vocabulary

`agent_runs.delivery_status` is written by `robothor/engine/delivery.py`
(`apply_receipt`), from the `SendReceipt` the channel returned — never from the
fact that a send was attempted. Announced output is routed by name through
`robothor/engine/channels/` (`AgentConfig.delivery_channel`, empty meaning
`telegram`), so the *send* happens in a channel wrapper while the *status* is
still decided in one place. `TelegramBot.send_message` swallows per-chunk
exceptions and returns one entry per chunk it managed to send (empty if all of
them failed), so the length of that list is the only evidence of delivery.

A channel may supply its own status when it knows something the counts do not —
a misconfiguration caught before the send, an exception, a publish the bus
refused. It may **not** use that to assert reach: a status claiming delivery on
an incomplete receipt is refused and recorded `failed:<channel>_unproven`, and
`delivered_at` is set only when the recorded status is `delivered`.

`<channel>` below is the agent's `delivery.channel` — `telegram` unless it names
another.

| Status | Meaning |
|--------|---------|
| `delivered` | Every chunk was acknowledged. Only this value sets `delivered_at`. |
| `partial:<sent>/<expected>` | Some chunks landed, the rest were lost — a truncated briefing, not a delivered one. |
| `published` | Event-bus mode, or `delivery.channel: event_bus`; the bus returned a stream message id. Never sets `delivered_at` — a stream write is not a person reading something. |
| `failed:<channel>_send` | The sender returned nothing: the recipient saw none of it. `failed:telegram_send` is the common case. |
| `failed:<channel>_exception: <err>` | The send raised. |
| `failed:<channel>_unproven` | Something claimed success without evidence: a sender that returned a value which is not a sequence of messages, or a channel whose status claimed delivery its own receipt did not support. |
| `failed:telegram_unproven` | A replacement for `delivery._deliver_telegram` returned without stamping `run.delivery_status`. See below. |
| `failed:<channel>_no_sender` / `failed:<channel>_no_target` / `failed:<channel>_unexpanded_target` | Misconfiguration caught before the send, on a channel built from a registered platform sender. |
| `failed:telegram_no_sender` / `failed:telegram_no_chat_id` / `failed:telegram_unexpanded_chat_id` | The same three for the built-in Telegram wrapper, under its historical names. |
| `failed:telegram_no_config` / `failed:telegram_no_run` | A channel `send()` was called without the config or run it needs. Programming error, recorded rather than raised. |
| `failed:slack_not_configured` / `failed:slack_unresolved_target` / `failed:slack_client:transport` / `failed:slack_client:dm_open` | The Slack channel is registered on every instance, configured or not. No bot token in the environment or the vault; a target that is not a Slack id (a `#name` cannot be posted to); the `slack_sdk` transport could not be built (usually the `channels` extra is not installed); or a `U…`/`W…` target could not be turned into a conversation. The reason is a CLOSED set of tokens, never the SDK's own text -- a status a query cannot match exactly is not a status, and an SDK error can carry a credential. See [the Slack channel page](channels/slack.md). |
| `failed:email_dnc` / `failed:email_dnc_unreadable` | The email channel refused before any transport existed. The first is a recipient flagged `crm_people.do_not_contact` (migration 113), filed in `agent_guardrail_events`; the second is an opt-out list that could not be READ, which is deliberately a different token — "we could not check" is not "nobody opted out", and overloading the first would make a database outage look like a wave of unsubscribes. `ROBOTHOR_DNC_MODE=observe` lets the mail go for **these two only**: the first still files its row (`action = 'observed'`), the second leaves an ERROR line and no row, because that write goes to the database the lookup just failed on. |
| `failed:email_no_transport` / `failed:email_send` / `failed:email_no_target` / `failed:email_unexpanded_target` / `failed:email_unresolved_target` / `failed:email_no_run` / `failed:email_benchmark` | The email channel is registered on every instance, configured or not. No `gws` CLI and no `ROBOTHOR_EMAIL_SMTP_HOST` + `ROBOTHOR_EMAIL_FROM` — or an SMTP configuration the channel refuses because it would send the password in the clear (`ROBOTHOR_EMAIL_SMTP_STARTTLS=false` on a submission port); a transport that refused, raised, or accepted the call without returning a message id; no `delivery.to`; a literal `${VAR}`; a target that is neither an address nor a `crm_people` id (or a person with no primary email); a send with no run, and so no tenant to scope the per-tenant opt-out list to; a benchmark run, which never mails because a sandbox tenant isolates the database and not the outside world. None of these is affected by `ROBOTHOR_DNC_MODE`. See [the email channel page](channels/email.md). |
| `failed:webchat_no_target` / `failed:webchat_unexpanded_target` / `failed:webchat_unknown_user` | The webchat channel was given no `delivery.to`, one that still contains `${…}`, or one that is not an active `user_accounts` row in this tenant. Nothing is written in any of the three. |
| `failed:webchat_no_session_write` / `failed:webchat_no_notification` / `failed:webchat_send` | A webchat send writes TWO rows — the assistant turn in the member's chat session and the notification in their inbox — so `expected` is 2 and these name which half is missing (the turn, the notification, or both). Half of a web-chat delivery is not a delivery; the halves are named separately because they have different fixes. See [the web chat page](channels/webchat.md). |
| `failed:event_bus_publish` / `failed:event_bus_disabled` / `failed:event_bus_exception: <err>` / `failed:event_bus_no_run` | The publish did not happen. |
| `failed:no_channel:<name>` | The agent's `delivery.channel` names a channel nothing is registered under. Delivery is refused rather than redirected to another surface. |
| `no_output`, `silent`, `suppressed_trivial`, `suppressed_sub_agent`, `blocked_by_hook:<reason>` | Nothing was meant to be sent. |

**`failed:telegram_unproven` is a deliberate behaviour change.** `_deliver_telegram`
is a seam instances monkeypatch to intercept outbound text. Before the channel
registry, a replacement that returned `True` and wrote nothing left
`delivery_status` NULL, and `_persist_delivery_status` early-returns on a falsy
status — so nothing was recorded at all. It is now recorded as a failure,
because a bare return value is not evidence that anything reached anybody, and
an unrecorded delivery is worse than a recorded failure. Production's
`TelegramBot.send_message` returns its landed messages, so this only bites a
replacement that does not: **a replacement must stamp `run.delivery_status`**
(the simplest way is to call the original it replaced).

**A thin announce reply falls back to the note the run wrote.** An announce
agent sometimes finishes its work, saves the result as a CRM note
(`create_note`, a 1,000+ character body) and then ends the run with a
meta-confirmation — `"Briefing delivered."`, 19 characters — so the operator
received a header with nothing under it. `run_finalizer._assess_outcome` has
always *flagged* that (`Thin announce output (N chars) — likely
meta-confirmation instead of full content`); `deliver()` now recovers from it.
When the mode is ANNOUNCE and the final text is thin by the same predicate
(`is_thin_announce_output` in `robothor/engine/thin_announce.py`, one threshold
shared with the finalizer — a module gate fails if any other engine file defines
it), the run's own steps are searched for a `create_note` call whose `body` is
itself substantial by that threshold, and that body is delivered in place of the
stub, under the header the channel already adds. With no such note the stub is
delivered as before and the finalizer's note stands.

The scope is exactly `create_note` steps whose `run_id` **is this run's**: an
unattributed step (an empty `run_id` on either side) and a note from another run
are both refused, and another tool's `body` argument is never eligible —
`gws_gmail_send` has one too, and an outbound email addressed to a third party is
not the operator's briefing. Where a run wrote several qualifying notes the
**most recently authored one wins** (the highest `step_number`, not the longest
body), so an agent that files a long research note and then the short final
briefing broadcasts the briefing. A `create_note` whose *save failed* still
supplies its body: the content is agent-authored and addressed to the operator,
and losing it is the defect this exists to fix.

`delivery_status` is recorded from the receipt exactly as for any other send, and
the substitution is written to `outcome_notes` **after** the send, from what was
actually checked — `substituted note body (saved) — delivered`,
`substituted note body (note save failed) — delivered`, or
`substituted note body — send failed: <status>` for anything the channel did not
fully acknowledge (`partial:…`, any `failed:…`). It never reads `delivered` on a
receipt that did not. One such note is kept per run: a later `deliver()` of the
same run replaces it rather than appending a second, contradicting one. The note
is what explains why the delivered text differs from `agent_runs.output_text`,
which keeps the stub as evidence.

Consumers must treat *only* `delivered` as reach: `analytics.py` counts it for
the delivery success rate, and `scheduler._maybe_emit_heartbeat_status_ping`
fires a fallback ping for everything else, so a `partial:` or `failed:` beat
still reaches the operator as a one-line health signal.

#### Asking a person: `Channel.ask`

`send` is one-way. `ask` is the other slot on the channel protocol
(`robothor/engine/channels/base.py`): put a question to the person at `target`
and wait. Two callers, one contract.

```
ask(question, options=(), *, timeout=300.0, target="", addressee="") -> str | None
```

`target` is **where** — the address the question is sent to. `addressee` is
**who** — the channel-native id of the person being asked. A channel that can
receive must bind its pending question to *both* and settle it only for an
answer matching both; getting that wrong is how an answer typed by one person
settles a question asked of another. An empty `addressee` means "whoever the
platform's own authorization says may answer here", which for Telegram is the
operator.

**`None` is the only non-answer, and it is never one of `options`.** A timeout,
an unreachable surface, or nobody to ask all return `None`; a channel that
returned a plausible choice because the clock ran out would be recording a
decision nobody made. `NotImplementedError` is also a legitimate outcome —
`EventBusChannel.ask` raises it, because a sink has nobody to ask — so **every
caller catches it** and falls through to whatever it does when no person is
reachable.

| Channel | `ask` |
|---------|-------|
| `telegram` | With `options` and a reachable aiogram `Bot`, an inline keyboard whose `callback_data` is `ask:<id>:<index>` — the index, because Telegram caps `callback_data` at 64 bytes and a long option would come back truncated into a different answer. With no bot to attach a keyboard to, the options go out numbered in the text and a typed `1`..`N` (or the option itself) answers it. Without options, plain text; `handle_text` intercepts the reply **before** `_enqueue_message`, which would otherwise buffer it until the blocked run finished. See the binding rule below for who may answer |
| `event_bus` | `NotImplementedError`, permanently |
| `webchat` | The run **waits — if somebody is listening**, and on the durable row rather than on a reply reaching this process (there is no inbound web-chat socket). `approval_required` (carrying the row id and options) goes out over the run's own SSE stream, the browser answers `POST /api/approvals/question/{id}` through the bridge, and the channel polls that row every ~2s until it is settled. Expiry or the deadline is `None`, never an option. When `emit_status` reports that **no sink took the event** the channel raises `NoListenerError` instead of waiting: the question reached no screen, so `ask_user` records `delivered: false` with `reason: no_listener` in the same tick rather than spending the tool budget and then claiming the person stayed silent. The bridge route is operator-gated, so a member sees the card and an honest refusal rather than a silent failure |

The row id reaches `webchat`'s `ask` through an **opt-in capability flag**, not
through the signature above: a channel that sets `ask_wants_question_id = True`
is additionally passed `question_id` and `run_id`. `TelegramChannel.ask` and
`SlackChannel.ask` accept no `**kw`, so an unconditional extra kwarg would raise
`TypeError`, be caught by `ask_user._ask_channel` as "this channel cannot ask",
and break every Telegram ask with every test still green.

**Who may answer.** An ask is bound at mint to `(chat_id, addressee)` and
settles only for an answer arriving from that chat **and** that sender
(`channels/telegram_ask.py`). The rule, in the order it is applied:

| The ask | What authorizes an answer |
|---------|---------------------------|
| Bound to an addressee, raised in that person's own chat | The bound chat and the bound sender. Nothing further — this is a registered non-owner answering their own agent's question |
| Bound to an addressee, raised in the operator's chat | The bound chat and the bound sender, **and** `_check_owner_gate` (site `ask_answer`) on top |
| No addressee — every permission escalation, raised for the operator by construction | The chat the prompt was sent to, **and** the owner gate. With nobody bound, the gate is the only authorization there is |

Refusals are one sentence for every reason (wrong chat, wrong sender, failed
gate, already answered) and a counted log line: naming which check failed tells
a forger how to pass it. The earlier cut authorized purely on
`chat_id == default_chat_id`, which both locked the addressee out of their own
question and let anyone in the operator's chat settle an ask registered
elsewhere — the ask id travels in `callback_data`, so nothing else was needed.

**Who asks.** `ask_user` (`tools/handlers/ask_user.py`) is the agent asking mid-
turn; it refuses on a run nobody is watching (cron, hooks, sub-agents) with an
explanatory error rather than being absent from the toolset.
`PermissionEscalationManager` (`permission_escalation.py`) is a guardrail
pausing a tool call; its Telegram keyboard and `perm:` callback data are
unchanged, and the channel is what it falls back to when the bot cannot deliver.

**What outlives the wait.** `ask_user` writes the `agent_questions` row *before*
the channel is asked, so a restart mid-ask does not lose the question and a late
answer is still usable. In-RAM escalations are the exception by design — they
are sub-minute and interactive — and the watchdog sweeps both halves every
minute (`daemon._sweep_stale_questions`): stale prompts are denied, overdue rows
are stamped `expired` and **kept**. The sweep reaps an escalation only once it
is past **its own** `human_approval_timeout`, never on a flat age: a
housekeeping tick must not be stricter than the budget the manifest declared,
and a prompt whose request is gone answers "no longer pending" rather than
confirming a decision the agent never received.

**`approval_required`.** Both askers emit this status event through
`robothor/engine/run_status.py`, a per-run sink the runner arms alongside the
live-session registry. Payload: `{event, kind: "escalation"|"question", id,
run_id, agent_id, tool?, question, options, expires_at}`. It is a notification —
the row (or the pending request) is the truth — and it is how a web client
learns there is something to answer.

#### Letting a person in: inbound access

`send` and `ask` are about reaching a person. The third question a channel has
to answer is whether a person may reach *it*, and before migration 118 each
channel answered differently: Telegram with a ladder of fabricated identities
inside `_resolve_user`, Slack with an `_authorized` that returned **true when
no allowlist was configured at all**.

`robothor/engine/channels/access.py` is now the one gate, with three modes —
`pairing`, `allowlist`, `open` — set per channel through
`ROBOTHOR_<CHANNEL>_ACCESS` (or `ROBOTHOR_CHANNEL_ACCESS_DEFAULT` for a plugin
channel). A known identity short-circuits every mode. An unknown sender on a
`pairing` channel is answered with a six-character one-shot code and nothing
else; the code is stored as a sha256, lives ten minutes, is spent by a single
`UPDATE … WHERE used_at IS NULL … RETURNING`, and is never minted on a group
surface.

**A channel message may never approve a pairing.** `approve_pairing` takes a
mandatory `actor` and refuses anything not prefixed `operator:` (the bridge's
operator gate) or `cli:` (a shell on the box), so the stranger who sends
`approve ABC234` back down the wire is refused structurally rather than by a
check somebody has to remember.

Telegram defaults to `open` purely for compatibility, and under `pairing` it
closes all three of its surfaces — private, group and the operator's own chat —
so only senders with a `tenant_users` row run. Slack defaults to `pairing`, with
one compatibility clause for instances carrying a pre-modes allowlist that turns
on whether the mode was *configured* rather than on what it resolves to. Because
a decision written by the bridge or the CLI cannot reach the engine's in-process
identity caches (60 s, and 300 s for `tenant_users`), both callers finish by
posting `/api/admin/identities/reload`, best effort. Full rules, tables and CLI:
[Channel access](channels/access.md).

**Answering from the Helm.** `GET /api/approvals` lists both durable kinds;
`POST /api/approvals/{kind}/{id}` answers one, with `kind` in `workflow`,
`question`, `escalation`. Operator-scoped and audited (identifiers only — the
answer text is content, not an identifier). `escalation` is proxied to the
engine, because settling it means waking a coroutine in that process.

### Voice & SMS (Twilio)

| Service | Port | Number |
|---------|------|--------|
| Voice server | 8765 | (your Twilio number) |
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

The supported, portable recovery contract is `robothor snapshot`; see
`docs/runbooks/SNAPSHOT_RESTORE.md`. It provides versioned manifests,
PostgreSQL custom dumps, workspace-state checksums, encrypted atomic output,
verification, retention, and guarded restore. The SSD procedure below is a
legacy instance-specific secondary copy and does not replace snapshot
verification or restore rehearsal.

The 15-minute RPO and 60-minute RTO are targets, not properties of the snapshot
format. The daily SSD schedule below cannot by itself demonstrate either target;
only scheduled off-site recovery points, age monitoring, and a timed restore
drill can do so.

LUKS2-encrypted SanDisk SSD (1.8 TB) mounted at `/mnt/robothor-backup`.

| Field | Value |
|-------|-------|
| Schedule | Daily 4:30 AM |
| Encryption | LUKS2, keyfile unlock (slot 0) + passphrase fallback (slot 1) |
| Retention | 30 days for database dumps |

### What's Backed Up

| Category | Contents |
|----------|----------|
| Project directories | the instance's `brain/`, plus `robothor/` (including `robothor/engine/`, `robothor/health/`) |
| Config directories | `.config/robothor/`, `.cloudflared/` |
| Credentials | `.bashrc`, the CRM stack's `.env` |
| Databases | `pg_dump`: robothor\_memory |
| Docker volumes | uptime-kuma-data |
| System state | crontab export, Ollama model list, systemd service files |
| Verification | SHA256 manifest of all backed-up files |

---

## Folder Structure

```
robothor/                                 Project root (git repo)
├── CLAUDE.md                             Master project guide
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
│   ├── docker-compose.yml                Uptime Kuma + Kokoro TTS
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
├── brain/ → ~/robothor/brain/                     Core workspace (symlink)
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
│   ├── USER.md                           Owner's profile
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
│   ├── schedule_reconcile.py             The wanted job set, as data (start + reconcile share it)
│   ├── admin_scheduler.py                POST /api/admin/scheduler/reconcile, GET /api/admin/tools
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

*Updated 2026-07-13.*

## Personal autonomous execution

The native vault's personal-resource broker (`robothor/autonomy/`) consumes
owner-scoped references in a separate browser process. The existing browser
tool exposes preparation, execution and reconciliation under standing grants.
Migration 127 adds encrypted `vault_resources`, wrapped `autonomy_key_versions`,
`autonomy_grants`, `autonomy_operations`, `autonomy_events`, and
`autonomy_settings`; service-secret exports exclude personal resources.
The account dashboard enrolls information and grants authority through
authenticated `/api/autonomy` endpoints. Existing organizational treasury stays
separate. See [Personal autonomous execution](AUTONOMOUS_EXECUTION.md) for
transaction state, revocation, key rotation and deployment requirements.

Migration 129 adds owner/agent-bound `autonomy_workflows` and durable command
results. `robothor-autonomy.service` owns persistent protected browser contexts,
with a private Unix socket and a separate JWT audience and scope. Engine and
bridge controllers pass resource references and signed identities; secure human
code entry sends its transient code only through this channel. Controller
restarts preserve pages, while broker loss preserves reservations for
reconciliation. See the workflow protocol and lifetime limits in the same guide.

Personal automation migration 131 adds `autonomy_enrollments`, scoped by tenant
and canonical person owner. It stores only a token hash, resource kind, website
origin, expiration and the completed vault resource reference. Resource creation
and consumption of the enrollment intent commit atomically. See
[private input enrollment](AUTONOMOUS_EXECUTION.md#private-input-enrollment).
