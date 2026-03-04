# CRON_DESIGN.md — Canonical Cron Architecture

**STATUS: FINAL — DO NOT MODIFY WITHOUT PHILIP'S APPROVAL**

---

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: System Crons (Python scripts via crontab)         │
│                                                             │
│  calendar_sync.py  → memory/calendar-log.json               │
│  email_sync.py     → memory/email-log.json                  │
│  jira_sync.py      → memory/jira-log.json                   │
│                                                             │
│  • Runs on exact schedule (*/5 min for email/calendar)      │
│  • Fetches data from APIs (gog, jira)                       │
│  • Writes entries with NULL notifier fields                 │
│  • No AI, no tokens, 100% mechanical                        │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼ logs with null fields
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2: Granular Agent Pipeline (Kimi K2.5)                │
│                                                             │
│  Email Classifier (hourly :00) — classify emails, route/escalate │
│  Calendar Monitor (*/15) — detect conflicts, changes        │
│  Email Analyst (hourly :30) — analyze complex emails        │
│  Email Responder (hourly :45) — compose and send replies    │
│  Conversation Inbox Monitor (*/30 6-22) — urgent message alerts │
│  Conversation Resolver (every 2h 6-22) — resolve stale convos   │
│  CRM Steward (10:00, 18:00) — data hygiene + enrichment    │
│                                                             │
│  • Each reads its own input (triage-inbox or task inbox)    │
│  • Escalates via create_task to main                        │
│  • Output: HEARTBEAT_OK if nothing to process               │
│  • Instructions: EMAIL_CLASSIFIER.md, CALENDAR_MONITOR.md,  │
│    RESPONDER.md, CONVERSATION_RESOLVER.md, CRM_STEWARD.md  │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼ *-status.md + task escalations
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3: supervisor_relay.py (Python, */10 min)             │
│                                                             │
│  • Meeting alerts within 20 min → directly to Telegram      │
│  • Stale worker / CRM health → writes to handoff.json       │
│    (for heartbeat to investigate and surface)               │
│  • Cooldowns: stale 60 min, CRM 30 min                      │
│  • Does NOT send escalations to Telegram                    │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LAYER 3.5: Main Heartbeat (Sonnet 4.6, 4h 6-22, TELEGRAM)  │
│                                                             │
│  • Reads all *-status.md files for worker activity          │
│  • Reads worker-handoff.json for unsurfaced escalations     │
│  • Investigates before surfacing (reads actual threads)     │
│  • Surfaces concise one-liners to Philip via Telegram       │
│  • HEARTBEAT_OK only when truly nothing to report           │
│  • Philip's sole gatekeeper — nothing else reaches him      │
│    except time-critical meeting alerts from relay           │
│  • Instructions: HEARTBEAT.md                               │
└─────────────────────────────────────────────────────────────┘
```

---

## System Crontab (Layer 1)

```crontab
# Calendar Sync - every 5 min
*/5 * * * * cd /home/philip/clawd && $W .../python scripts/calendar_sync.py

# Email Sync - every 5 min
*/5 * * * * cd /home/philip/clawd && $W .../python scripts/email_sync.py

# Jira Sync - every 30 min during work hours (M-F)
*/30 6-22 * * 1-5 cd /home/philip/clawd && $W .../python scripts/jira_sync.py

# Memory maintenance (3 AM) - TTL expiry, archival
0 3 * * * /home/philip/clawd/memory_system/maintenance.sh

# Intelligence Pipeline — Three Tiers
*/10 * * * * .../python continuous_ingest.py     # Tier 1: every 10 min
0 7,11,15,19 * * * .../python periodic_analysis.py  # Tier 2: 4x daily
30 3 * * * .../python intelligence_pipeline.py       # Tier 3: daily 3:30 AM

# Hourly support
20 * * * * .../python3 -c "..." # clear response-analysis.json
25 * * * * .../python scripts/email_response_prep.py         # enrich + depth tag
*/10 6-23 * * * .../python scripts/supervisor_relay.py        # relay to handoff
0 * * * * .../python scripts/system_health_check.py           # hourly health
```

(Paths abbreviated. All wrapped with `$W` = `cron-wrapper.sh` for SOPS credential injection.)

### SOPS Credential Injection

All cron jobs that need credentials are wrapped with `cron-wrapper.sh`:
```
W=/home/philip/robothor/scripts/cron-wrapper.sh
*/5 * * * * cd /home/philip/clawd && $W /home/philip/clawd/memory_system/venv/bin/python scripts/email_sync.py
```

The wrapper sources `/run/robothor/secrets.env` (decrypted from SOPS at boot) before executing the command. This injects all environment variables (GOG_KEYRING_PASSWORD, TELEGRAM_BOT_TOKEN, PG_PASSWORD, etc.) without hardcoding them.

Jobs that don't need credentials (find cleanup, backup) run without the wrapper.

### Intelligence Pipeline Architecture

The intelligence pipeline uses a three-tier architecture to eliminate the 18.5-hour blind spot:

| Tier | Script | Schedule | Purpose | Duration |
|------|--------|----------|---------|----------|
| 1 | `continuous_ingest.py` | */10 min | Incremental deduped ingestion from all sources | 0-3 min |
| 2 | `periodic_analysis.py` | 4x daily | Meeting prep, memory blocks, entity enrichment | 3-8 min |
| 3 | `intelligence_pipeline.py` | Daily 3:30 AM | Relationships, engagement, patterns, quality | ~23 min |

**Dedup:** `ingested_items` table tracks (source, item_id, content_hash). Same-hash items are skipped.
**Locking:** `fcntl.flock()` prevents concurrent Tier 1 runs. Tier 1 skips when Tier 3 is active.
**Errors:** 3+ consecutive failures per source escalate to `worker-handoff.json`.


---

## Agent Engine Configuration (Layer 2)

### Granular Agent Pipeline

Specialized agents, each with its own agentId for task isolation. Only 3 agents deliver to Telegram (`delivery: announce`). All worker agents use `delivery: none` (agents still run, output just isn't delivered). Models vary per agent — see YAML manifests in `docs/agents/`.

| Agent | Schedule | Input | Output | Instructions |
|-------|----------|-------|--------|-------------|
| Agent | agentId | Schedule | Input | Output | Instructions |
|-------|---------|----------|-------|--------|-------------|
| Email Classifier | `email-classifier` | `0 6-22 * * *` | triage-inbox.json | email-classifier-status.md | EMAIL_CLASSIFIER.md |
| Calendar Monitor | `calendar-monitor` | `*/15 6-22 * * *` | triage-inbox.json | calendar-monitor-status.md | CALENDAR_MONITOR.md |
| Email Analyst | `email-analyst` | `30 6-22 * * *` | task inbox (analytical) | response-analysis.json + email-analyst-status.md | EMAIL_ANALYST.md |
| Email Responder | `email-responder` | `45 6-22 * * *` | task inbox (reply-needed) | response-status.md | RESPONDER.md |
| Conversation Inbox Monitor | `conversation-inbox` | `*/30 6-22 * * *` | CRM API | conversation-inbox-status.md | (inline in job payload) |
| Conversation Resolver | `conversation-resolver` | `0 6-22/2 * * *` | CRM API | conversation-resolver-status.md | CONVERSATION_RESOLVER.md |
| CRM Steward | `crm-steward` | `0 10,18 * * *` | CRM API + DB | crm-steward-status.md | CRM_STEWARD.md |

All agents escalate via `create_task(assignedToAgent="main")` and output `HEARTBEAT_OK` when nothing to process. `worker-handoff.json` is still read by the heartbeat for infrastructure alerts from Python scripts, but agents no longer write to it.

```json
{
  "agentId": "email-classifier",
  "name": "Email Classifier",
  "schedule": { "kind": "cron", "expr": "0 6-22 * * *", "tz": "America/New_York" },
  "sessionTarget": "isolated",
  "delivery": { "mode": "announce" }
}
```

### Supervisor Relay (Layer 3)

Python script (`scripts/supervisor_relay.py`), runs every 10 minutes via crontab. Handles:
- **Meeting alerts within 20 min** → sends directly to Telegram (can't wait for supervisor cycle)
- **Stale worker / CRM health** → writes escalation dicts to `worker-handoff.json` for heartbeat

Does NOT send escalations, worker output, or health alerts to Telegram.

### Main Heartbeat (Layer 3.5)

Runs every 4h (6-22h) on Telegram. Philip's sole gatekeeper — investigates escalations before surfacing. Reads all worker status files + worker-handoff.json (for infrastructure alerts) + agent task inbox. Defined as `heartbeat:` block in `main.yaml`.

```json
{
  "agentId": "main",
  "heartbeat": {
    "cron": "0 6-22/4 * * *",
    "timezone": "America/New_York",
    "instruction_file": "brain/HEARTBEAT.md",
    "session_target": "isolated",
    "delivery": { "mode": "announce", "channel": "telegram" }
  }
}
```

See `HEARTBEAT.md` for full protocol.

### SystemEvent Crons

| Name | Schedule | Payload |
|------|----------|---------|
| Morning Briefing | 6:30 AM daily | Generate daily briefing |
| Evening Wind-Down | 9:00 PM daily | Tomorrow preview |

These are `sessionTarget: isolated` with `delivery: announce` on Telegram.

---

## Handoff File Schema

`memory/worker-handoff.json` is the communication channel between the triage worker and supervisor:

```json
{
  "lastRunAt": "2026-02-06T12:00:00Z",
  "lastRunStatus": "ok",
  "escalations": [
    {
      "id": "<uuid>",
      "source": "email|calendar|jira|vision|conversation|crm-steward|health_check|relay",
      "sourceId": "<log entry id>",
      "reason": "Why this needs Philip's attention",
      "summary": "Brief description",
      "urgency": "high|critical",
      "createdAt": "2026-02-06T12:00:00Z",
      "surfacedAt": null,
      "resolvedAt": null
    }
  ],
  "summary": {
    "emailsProcessed": 0,
    "emailsEscalated": 0,
    "calendarEventsChecked": 0,
    "jiraTicketsChecked": 0
  },
  "healthCheck": {
    "emailApiOk": true,
    "calendarApiOk": true,
    "jiraApiOk": true
  }
}
```

**Escalation lifecycle:**
1. Worker/relay adds escalation with `surfacedAt: null, resolvedAt: null`
2. Heartbeat investigates (reads threads, checks health, etc.)
3. Heartbeat surfaces to Philip via Telegram, sets `surfacedAt` timestamp
4. Philip responds or heartbeat takes action, sets `resolvedAt` timestamp
5. Escalation is complete when both `surfacedAt` and `resolvedAt` have timestamps
6. Auto-resolve: bridge watchdog and health checks can set `resolvedAt` when issues self-heal

---

## Log Entry Lifecycle

Every entry has notifier fields that track state:

| Field | null | timestamp | Meaning |
|-------|------|-----------|---------|
| `readAt` | Not read | When read | Email fetched (email only) |
| `categorizedAt` | Needs categorization | When categorized | Stage 1 complete |
| `actionRequired` | Needs decision | N/A (string value) | What to do |
| `actionCompletedAt` | No action yet | When action taken | Stage 2 complete |
| `pendingReviewAt` | Not pending | When marked for review | Awaiting verification |
| `reviewedAt` | Not verified | When verified | Stage 3 complete |

**Flow:**
```
null → read → categorize → act → pendingReviewAt → verify → reviewedAt
```

**Rule:** Entry is not complete until `reviewedAt` has a timestamp.

---

## Key Rules

- **Each agent has its own agentId** (e.g., `email-classifier`, `calendar-monitor`) for task isolation
- **Pipeline agents use `delivery: announce`** — output delivered to Telegram
- **Background agents use `delivery: none`** — agents still run, output just isn't delivered
- Crons run as isolated sessions (`sessionTarget: "isolated"`)
- Output `HEARTBEAT_OK` when nothing urgent (gets suppressed by announce mode)
- Output alert text when something needs Philip's attention
- **Models vary per agent** — see jobs.json for actual per-agent model assignments

---

## Log Schemas

See `scripts/schemas/log-schemas.md` for full field specifications.

---

## Event-Driven Hooks (Engine)

The Agent Engine's Redis Stream hooks (`robothor/engine/hooks.py`) are the primary trigger for email, calendar, and vision agents. Crons are relaxed safety nets (6h cadence).

```
EVENT-DRIVEN (primary, ~60s email-to-classification):
  email_sync.py publishes → robothor:events:email
  hooks.py consumes → triggers email-classifier agent
  Engine workflow (email-pipeline) chains: classify → condition → analyze/respond

CRON (safety net, 2-6h cadence):
  Each agent has a relaxed cron schedule as fallback
```

### Key Design Decisions

- **Crons are safety nets.** Primary trigger is event hooks. Crons run at 2-6h intervals.
- **Every agent is idempotent.** When hooks trigger an agent, the cron finds nothing to do.
- **Engine workflows** (`docs/workflows/*.yaml`) chain multi-step pipelines with conditional routing.
- **Dedup:** Engine uses `max_instances=1` + dedup keys to prevent concurrent runs.

---

## This Is Final

This document defines the architecture. All components follow this pattern.

- **System crons** (Python) = data fetching, intelligence pipeline, scheduled exact
- **Granular agents** (per-agent IDs, isolated crons) = Email Classifier, Calendar Monitor, Email Analyst, Email Responder, Conversation Inbox Monitor, Conversation Resolver, CRM Steward — each writes *-status.md + escalates via tasks
- **Main heartbeat** (Sonnet 4.6, 4h 6-22, on Telegram) = investigate escalations, surface to Philip
- **SystemEvent crons** (Engine) = briefings at specific times (Morning, Evening)
- **Event-driven hooks** (Engine, Redis Streams) = primary trigger for email, calendar, vision agents

## Routines — Database-Driven Scheduling

In addition to cron expressions in `jobs.json`, the Bridge supports **routines** — recurring task templates stored in `crm_routines`.

- **Table:** `crm_routines` — stores title, body, cron expression, assigned agent, priority, tags, and tenant
- **Trigger loop:** Bridge runs a 60-second interval loop that checks for due routines and creates tasks
- **Dedup:** Won't create a task if an open task with the same title already exists for that routine
- **Use case:** Scheduled maintenance tasks, periodic reviews, recurring check-ins that don't need a full OpenClaw cron job

This is complementary to `jobs.json` — routines create *tasks* (lightweight, tracked in CRM), while cron jobs create *agent sessions* (full LLM context, tools, output delivery).

---

**Created:** 2026-02-05
**Updated:** 2026-03-04 (removed dead triage_prep/cleanup/moltbot, updated to Engine architecture)
**Status:** CANONICAL
