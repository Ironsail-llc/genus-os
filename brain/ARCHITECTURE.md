# ARCHITECTURE.md — Robothor System Design

## Core Principle

**System crons fetch data. Intelligence pipeline processes it into memory. Triage worker handles routine items. Heartbeat surfaces what matters to Philip.**

---

## Six-Layer Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: System Crons (Python scripts via crontab)         │
│                                                             │
│  calendar_sync.py  → memory/calendar-log.json    (*/5 min)  │
│  email_sync.py     → memory/email-log.json       (*/5 min)  │
│  jira_sync.py      → memory/jira-log.json  (*/30 M-F 6-22) │
│  meet_transcript_sync.py → meet-transcripts.json (*/10 min) │
│  garmin_sync.py    → garmin-health.md            (*/15 min) │
│  vision_service.py → POST /ingest (camera)        (always)  │
│                                                             │
│  • Fetches data from APIs (Google, Jira, Garmin, Drive)    │
│  • Writes entries with NULL notifier fields                 │
│  • No AI, no tokens, 100% mechanical                        │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼ logs with null fields
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1.5: Intelligence Pipeline (Python, crontab)         │
│                                                             │
│  Tier 1: continuous_ingest.py (*/10 min)                    │
│    → Deduped ingestion from all sources into RAG memory     │
│    → ~10 min freshness, content_hash dedup                  │
│                                                             │
│  Tier 2: periodic_analysis.py (4x daily: 7,11,15,19)       │
│    → Phase 1: Meeting prep briefs                           │
│    → Phase 2: Memory block updates                          │
│    → Phase 3: Entity graph enrichment                       │
│    → Phase 4: Contact reconciliation + CRM discovery        │
│                                                             │
│  Tier 3: intelligence_pipeline.py (daily 3:30 AM)           │
│    → Phase 1: Catch-up ingestion                            │
│    → Phase 2: Relationship intelligence                     │
│    → Phase 2.5: Contact enrichment (fill CRM fields)        │
│    → Phase 3: Engagement scoring                            │
│    → Phase 4: Communication patterns                        │
│    → Phase 5: Memory quality audit                          │
│    → Phase 6: Cleanup + pruning                             │
│                                                             │
│  triage_prep.py (:14,:29,:44,:59) — 1 min before worker     │
│    → Extracts pending items → triage-inbox.json             │
│    → Enriches with contact context from PostgreSQL          │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼ triage-inbox.json (enriched, small)
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2: Triage Worker (Kimi K2.5, */15 min, isolated)     │
│                                                             │
│  • Reads triage-inbox.json (NOT full log files)             │
│  • Categorizes entries (urgency, category, actionRequired)  │
│  • Handles routine items directly (mark read, respond)      │
│  • Escalates complex items → worker-handoff.json            │
│  • Writes triage-status.md (run summary for supervisor)     │
│  • Instructions: WORKER.md                                  │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼ triage-status.md + worker-handoff.json
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2.5: triage_cleanup.py (Python, 5 min after worker)  │
│                                                             │
│  • Marks processed items in log files                       │
│  • Updates heartbeat timestamp                              │
│  • Prevents false stale-worker alerts                       │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3: supervisor_relay.py (Python, */10 min 6-23h)      │
│                                                             │
│  • Meeting alerts within 20 min → directly to Telegram      │
│  • Stale worker / CRM health → writes to handoff.json       │
│    (for supervisor to investigate, NOT sent to Philip)      │
│  • Cooldowns: stale 60 min, CRM 30 min                      │
│  • Respects waking hours (7 AM - 10 PM ET)                │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3.5: Main Heartbeat (Sonnet 4.6, 4h 6-22, TELEGRAM)  │
│                                                             │
│  • Philip's sole gatekeeper — nothing else reaches him      │
│    except time-critical meeting alerts from relay           │
│  • Reads *-status.md for worker activity                    │
│  • Reads worker-handoff.json for unsurfaced escalations     │
│  • Investigates before surfacing (reads actual threads)     │
│  • Surfaces concise one-liners to Philip via Telegram       │
│  • HEARTBEAT_OK only when truly nothing to report           │
│  • Instructions: HEARTBEAT.md                               │
└─────────────────────────────────────────────────────────────┘
```

---

## Intelligence Pipeline

Three tiers of automated intelligence, no AI tokens (uses local Llama/Qwen3):

| Tier | Script | Schedule | Purpose | Duration |
|------|--------|----------|---------|----------|
| 1 | `continuous_ingest.py` | */10 min | Incremental deduped ingestion from all sources | 0-3 min |
| 2 | `periodic_analysis.py` | 4x daily (7,11,15,19) | Meeting prep, memory blocks, entities, contact reconciliation | 3-8 min |
| 3 | `intelligence_pipeline.py` | Daily 3:30 AM | Relationships, enrichment, engagement, patterns, quality | ~23 min |

### periodic_analysis.py Phases (Tier 2)

| Phase | What | LLM? |
|-------|------|------|
| 1 | Meeting prep briefs for upcoming meetings | Yes (Llama) |
| 2 | Memory block updates (user_profile, working_context, etc.) | Yes (Llama) |
| 3 | Entity graph enrichment from recent facts | Yes (Llama) |
| 4 | Contact reconciliation: link memory_entity_id, discover new CRM contacts | No (algorithmic) |

### intelligence_pipeline.py Phases (Tier 3)

| Phase | What | LLM? |
|-------|------|------|
| 1 | Catch-up: retry failed ingestion sources | No |
| 2 | Relationship intelligence: contact briefs | Yes (Llama) |
| 2.5 | Contact enrichment: fill CRM fields (job title, company, city) | Mixed (deterministic + Llama) |
| 3 | Engagement scoring | Yes (Llama) |
| 4 | Communication patterns | Yes (Llama) |
| 5 | Memory quality audit | Yes (Llama) |
| 6 | Cleanup: prune stale facts, dedup | No |

### Contact Enrichment Pipeline

```
Data Sources (already flowing)
├── meet-transcripts.json → attendee names, who-said-what
├── email-log.json → sender email, domain → company
├── calendar-log.json → attendee emails
├── memory_facts → 900+ facts (contacts, decisions, personal)
└── CRM conversations → interaction history

             ↓ (4x daily, periodic_analysis.py Phase 4)

Contact Reconciliation (algorithmic, no LLM)
├── Fuzzy-match memory_entities ↔ contact_identifiers → fill memory_entity_id
├── Scan meeting attendees + high-mention entities → discover new contacts
├── Filter reversed names (Gemini Notes artifacts) and service accounts
└── Create missing CRM records via DAL

             ↓ (daily, intelligence_pipeline.py Phase 2.5)

Contact Enrichment (deterministic + LLM)
├── Find CRM contacts with empty jobTitle/company/city
├── Deterministic: email domain → company (e.g., @getdrx.com → GetDRx)
├── LLM: extract job title, city from memory facts + meeting transcripts
├── Confidence threshold 0.7 — skip uncertain extractions
└── Update CRM (only fill empty fields, never overwrite)
```

**Key module:** `memory_system/contact_matching.py` — pure-Python name matching (normalize, similarity scoring, best-match with mention count tiebreaking).

**Dedup:** `ingested_items` table tracks (source, item_id, content_hash). Same-hash items are skipped.
**Locking:** `fcntl.flock()` prevents concurrent Tier 1 runs. Tier 1 skips when Tier 3 is active.

---

## Data Flow

```
┌──────────────────────────────────────────────────────────────┐
│                      EXTERNAL SOURCES                         │
│  Google Calendar │ Gmail │ Jira │ Garmin │ Camera │ Drive     │
└────────┬─────────────┬────────┬─────────┬──────────┬─────────┘
         │             │        │         │          │
         ▼             ▼        ▼         ▼          ▼
┌──────────────────────────────────────────────────────────────┐
│              LAYER 1: SYSTEM CRONS (Python)                   │
│    calendar_sync │ email_sync │ jira_sync │ garmin_sync       │
│    meet_transcript_sync │ vision_service (always-on)          │
│         │                │            │                       │
│         ▼                ▼            ▼                       │
│   JSON log files + meet-transcripts.json                      │
│   (null notifier fields — needs processing)                   │
└──────────────────────────────┬────────────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
   Intelligence Pipeline    triage_prep.py     Memory System
   (Tier 1/2/3)            (1 min before        (RAG, facts,
   RAG ingestion,           worker)              entities)
   meeting prep,           Extracts pending,
   contact enrichment      enriches with DB
            │                  │
            │                  ▼
            │          Triage Worker (Kimi K2.5)
            │          → categorize, act, escalate
            │                  │
            │          triage_cleanup.py
            │                  │
            │          supervisor_relay.py
            │          → meeting alerts to Telegram
            │                  │
            │          Main Heartbeat (Sonnet 4.6, 4h)
            │          → investigate + surface to Philip
            │                  │
            │                  ▼
            │          ┌──────────────────┐
            │          │ PHILIP (Telegram) │
            │          └──────────────────┘
            │
            ▼
   CRM (native PostgreSQL crm_* tables)
   ← contact_identifiers bridge
   ← memory_entity_id links
   ← enriched fields (job title, company, city)
```

---

## Log Entry Lifecycle

Every entry has notifier fields that track processing state:

```
null → categorize → act → pendingReviewAt → verify → reviewedAt
```

| Field | null | timestamp | Meaning |
|-------|------|-----------|---------|
| `categorizedAt` | Needs processing | When categorized | Triage worker processed |
| `actionRequired` | Needs decision | string value | What action to take |
| `actionCompletedAt` | Action pending | When completed | Action was taken |
| `pendingReviewAt` | Not flagged | When flagged | Awaiting verification |
| `reviewedAt` | Not verified | When verified | Entry complete |

**Rule:** Entry is not complete until `reviewedAt` has a timestamp.

### Escalation Lifecycle

```
worker creates → heartbeat surfaces → heartbeat resolves
```

| Field | null | timestamp | Meaning |
|-------|------|-----------|---------|
| `surfacedAt` | Not yet shown to Philip | When surfaced | Philip was notified |
| `resolvedAt` | Action not yet taken | When resolved | Action completed |

**Rule:** Escalation is not complete until both `surfacedAt` and `resolvedAt` have timestamps.

---

## Current Jobs

### System Crons (Layer 1 — Python, mechanical)

| Job | Schedule | Script | Output |
|-----|----------|--------|--------|
| Calendar Sync | */5 min | calendar_sync.py | memory/calendar-log.json |
| Email Sync | */5 min | email_sync.py | memory/email-log.json |
| Jira Sync | */30 min M-F 6-22h | jira_sync.py | memory/jira-log.json |
| Meet Transcript Sync | */10 min | meet_transcript_sync.py | memory/meet-transcripts.json |
| Garmin Sync | */15 min | garmin_sync.py | garmin-health.md |
| Continuous Ingestion (Tier 1) | */10 min | continuous_ingest.py | RAG memory |
| Periodic Analysis (Tier 2) | 0 7,11,15,19 | periodic_analysis.py | Meeting briefs, entities, contacts |
| Intelligence Pipeline (Tier 3) | 3:30 AM | intelligence_pipeline.py | Relationships, enrichment, patterns |
| CRM Consistency | 3:15 AM | crm_consistency.py | Cross-system checks |
| Maintenance | 3:00 AM | maintenance.sh | TTL expiry, archival |
| Snapshot Cleanup | 4:00 AM | find + delete | Delete >30 day snapshots |
| Data Archival | Sunday 4:00 AM | data_archival.py | Archive old data |
| Weekly Review | Sunday 5:00 AM | weekly_review.py | Deep synthesis report |
| SSD Backup | Sunday 4:15 AM | backup-ssd.sh | rsync + pg_dump |
| System Health Check | Hourly | system_health_check.py | Infrastructure health |
| Triage Prep | :14,:29,:44,:59 | triage_prep.py | triage-inbox.json |
| Triage Cleanup | :05,:20,:35,:50 | triage_cleanup.py | Mark processed items |
| Supervisor Relay | */10 min 6-23h | supervisor_relay.py | Meeting alerts, handoff |
| Vision Service | always-on | vision_service.py (systemd) | YOLO + face detection |

### OpenClaw Crons (Layer 2/3.5 — Kimi K2.5, Opus 4.6 fallback)

| Job | Schedule | Session | Purpose |
|-----|----------|---------|---------|
| Triage Worker | */15 min | isolated | Process triage-inbox, categorize, act, escalate |
| Main Heartbeat | every 4h 6-22h | isolated → telegram | Investigate + surface escalations |
| Vision Monitor | */10 min 7-23h | isolated | Check motion events, alert on visitors |
| Morning Briefing | 6:30 AM daily | isolated → telegram | Daily briefing |
| Evening Wind-Down | 9:00 PM daily | isolated → telegram | Tomorrow preview, open items |

---

## External Access

All internal services run on `127.0.0.1`. External access through Cloudflare tunnel only.

```
Internet → Cloudflare Edge → Cloudflare Tunnel → localhost
                │
                ├─ cam.robothor.ai ──────→ :8890 (HLS webcam)   [Access protected]
                ├─ robothor.ai ──────────→ :3000 (homepage)      [Public]
                ├─ status.robothor.ai ───→ :3001 (dashboard)     [Public]
                ├─ dashboard.robothor.ai → :3001 (alias)         [Public]
                ├─ privacy.robothor.ai ──→ :3002 (privacy)       [Public]
                ├─ ops.robothor.ai ──────→ :3003 (ops dashboard) [Access protected]
                ├─ voice.robothor.ai ────→ :8765 (Twilio voice)  [Public]
                ├─ sms.robothor.ai ──────→ :8766 (Twilio SMS)    [Public]
                ├─ gateway.robothor.ai ──→ :18789 (OpenClaw)     [Access protected]
                ├─ bridge.robothor.ai ───→ :9100 (Bridge API)    [Access protected]
                ├─ orchestrator.robothor.ai → :9099 (RAG)        [Access protected]
                ├─ vision.robothor.ai ───→ :8600 (Vision API)    [Access protected]
                └─ * ───────────────────→ 404
```

---

## Key Rules

1. **Layer 1 is mechanical** — Python scripts fetch data, no AI
2. **Intelligence pipeline feeds memory** — three tiers, all local models, no API costs
3. **Triage worker processes** — categorizes, acts on routine items, escalates complex ones
4. **Heartbeat is sole gatekeeper** — investigates before surfacing to Philip via Telegram
5. **Contact enrichment is automated** — reconciliation 4x daily, CRM enrichment daily
6. **Log everything** — every decision, action, and escalation gets a timestamp
7. **Escalate when unsure** — don't guess on important decisions
8. **Model: Kimi K2.5** (via OpenRouter) for all agent work. Opus 4.6 is first fallback. Local Llama/Qwen3 for RAG and intelligence pipeline.

---

## Anti-Patterns (Don't Do These)

- **Triage worker just logs, waits for supervisor to act** — adds delay
- **Heartbeat re-processes raw data** — duplicates triage worker's job
- **No escalation path** — workers shouldn't guess on ambiguous situations
- **Actions without timestamps** — can't verify what happened
- **Overwrite existing CRM data** — enrichment only fills empty fields
- **Heartbeat sends directly to Telegram** — goes through relay/handoff
- **Stale log entries** — every entry must reach `reviewedAt`; heartbeat catches stragglers

---

**Created:** 2026-02-05
**Updated:** 2026-02-15 (added intelligence pipeline tiers, contact enrichment, CRM routes, correct model refs)
