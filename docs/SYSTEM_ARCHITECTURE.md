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

Migrations: `065_session_goal_meta.sql` (column), `066_session_goal_meta_v2.sql` (v2 shape doc). `brain/GOAL.md` is a denormalized read-cache regenerated on every mutation; hand-edits are advisory only.

**Manifest goals are SEED, not source of truth.** `docs/agents/<agent>.yaml` `goals:` blocks supply the initial `metric_targets` when an agent's goal task is first created (via `scripts/seed_agent_goals.py` or lazily on first read). After seeding, the unified task is canonical and edits go through the CLI/Telegram/tools — manifest changes are advisory.

**Composition.** `robothor/engine/goals.py::compose_goals(agent_id, manifest, tenant_id)` returns the merged `GoalSpec` list every consumer reads from:
- The unified task's `metric_targets` (if any), else falls back to the manifest's `goals:` block.
- A synthetic `session-goal-alignment` GoalSpec (metric `session_goal_alignment_score`, target from the task's `alignment_target`, weight 5.0) when an objective is set.
- A synthetic `session-goal-progress` GoalSpec (validated_evidence / criteria_count, target `>=1.0`) when criteria exist.

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

### Review — `robothor/engine/buddy_critic.py`

The `buddy` agent (`docs/agents/buddy.yaml`, cron `0 6-22 * * *`) runs two passes:

- **Hourly review pass** — for each agent with goals, sample up to 2 recent top-level runs biased toward failures / error steps / long durations and not already reviewed. Build a structured `Evidence` dict from `agent_runs` + `agent_run_steps`. Sonnet 4.6 phrases a rating (1-5) + dimension + `specific_issue` (≤ 80 chars referencing concrete evidence) + `suggested_action` (≤ 120 chars). Persist to `agent_reviews` with `reviewer_type='buddy'`.
- **6-hourly aggregation pass** — run `detect_goal_breach` per agent. For breaches with `priority_score ≥ 3.0` *and* a non-null current metric value, build a `Finding`: 3 representative reviews, corrective-action template from `docs/agents/corrective-actions.yaml`, live baseline metric. Create one `crm_tasks` row per finding tagged `nightwatch+self-improve+<agent>+<metric>` assigned to `auto-agent`. Dedups against open tasks for the same (agent, metric). The task body embeds a machine-readable `<!-- buddy-baseline: {...} -->` marker the grader parses later.

The LLM receives pre-computed evidence and is used only to phrase the finding,
which reduces but does not eliminate hallucination risk. Persisted evidence and
post-change metrics remain the authority.

### Verify — `robothor/engine/buddy_grader.py`

The `buddy-grader` agent (`docs/agents/buddy-grader.yaml`, cron `7 * * * *`) closes the loop:

1. For every DONE self-improve task older than 48 hours with no verification tag yet, parse the baseline marker and re-run `compute_goal_metrics` for that metric.
2. Metric satisfies target → tag `verified_resolved` + resolution note.
3. Metric still breached → tag `verify_failed` + increment `escalation:N`, transition back to IN_PROGRESS. At `escalation:2` the task re-routes to `auto-researcher`; at `escalation:3` it's tagged `requires_human=true` and auto-escalation stops. This terminal state is mandatory — endless churn is worse than a known-open issue.
4. Separately, 7 days after `verified_resolved`, re-check the metric and tag `held_7d=true|false`. That's the data source for the weekly guardrail.

Env flag `ROBOTHOR_BUDDY_GRADER_DRYRUN=1` computes verdicts without writing, for operator-driven simulation.

### Guardrail — `robothor/engine/buddy_auditor.py`

The `buddy-auditor` agent (`docs/agents/buddy-auditor.yaml`, cron `0 7 * * 1`) is the falsifiability clause. Weekly, it reads the `held_7d=true|false` tag distribution over the last 14 days. If **under 30%** of fixes held for 7 days (min 5 samples), it pauses Buddy's cron by editing `docs/agents/buddy.yaml` and sends a critical alert to `main`. Re-enabling is a deliberate human decision.

Piggybacked on the same weekly run: the review-quality sentinel (`brain/scripts/buddy_review_quality_sentinel.py`) flags filler output if ≥ 20% of recent Buddy reviews fail a concrete-evidence heuristic.

### Observability

- `GET /api/buddy/ratings` — per-agent latest achievement + 7-day trend.
- `GET /api/buddy/reviews` — recent Buddy reviews, paginated.
- `GET /api/buddy/findings` — open/in-progress/verifying/resolved/persistent/requires_human buckets.
- `GET /api/buddy/verifications` — verified tasks with baseline → current → held_7d for the auditor.
- `brain/journals/buddy/YYYY-MM-DD.jsonl` — append-only audit trail of every review, finding, verification, hold-check, and audit.

### What was deleted

`buddy_watch.py` (parallel LLM cron), `_maybe_append_buddy_reflection` in `delivery.py` (heartbeat appendix), `_buddy_status_context` warmup hook, `flag_underperformers` + escalation mechanics in `buddy.py`, XP/level/streak gamification (constants, LevelInfo, DailyStats dataclasses), `improvement-analyst` agent + workflow (subsumed by `buddy`'s aggregation pass). Legacy `docs/workflows/nightwatch.yaml` is retired.

---

## Vision System

When operated continuously, the computer-vision service supports three modes:

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
- **`resolve_identity(channel, identifier, tenant_id) -> IdentityContext`**
  resolves any channel-native id (webchat account UUID, Telegram user id,
  a recognized face label) down to a common shape — `role`, `person_id`,
  `verified`, etc. — used uniformly by prompt assembly, permissions, and
  audit.
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
| Project directories | `brain/`, `robothor/` (including `robothor/engine/`, `robothor/health/`) |
| Config directories | `.config/robothor/`, `.cloudflared/` |
| Credentials | `.bashrc`, `crm/.env` |
| Databases | `pg_dump`: robothor\_memory |
| Docker volumes | uptime-kuma-data |
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
