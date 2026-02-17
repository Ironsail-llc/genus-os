# Data Flow

End-to-end flow from external APIs to Philip.

Canonical reference: `brain/ARCHITECTURE.md`

```
┌──────────────────────────────────────────────────────────────┐
│                      EXTERNAL SOURCES                         │
│  Google Calendar │ Gmail │ Jira │ Garmin │ Camera │ Drive     │
└────────┬─────────────┬────────┬─────────┬──────────┬─────────┘
         │             │        │         │          │
         ▼             ▼        ▼         ▼          ▼
┌──────────────────────────────────────────────────────────────┐
│              LAYER 1: SYSTEM CRONS (Python)                   │
│              No AI, no tokens, 100% mechanical                │
│                                                              │
│  calendar_sync.py  email_sync.py  jira_sync.py               │
│       */5 min          */5 min     */30 min M-F               │
│  meet_transcript_sync.py          garmin_sync.py              │
│       */10 min                       */15 min                 │
│          │                │            │                      │
│          ▼                ▼            ▼                      │
│  calendar-log.json  email-log.json  jira-log.json            │
│  meet-transcripts.json  garmin-health.md                      │
│  (null notifier fields — needs processing)                    │
│                                                              │
│  vision_service.py (always-on) → POST /ingest (camera)       │
│  system_health_check.py (hourly) → infrastructure health     │
└──────────────────────────────┬────────────────────────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
┌──────────────────────────────────────────────────────────────┐
│         LAYER 1.5: INTELLIGENCE PIPELINE (Python)             │
│                                                              │
│  Tier 1: continuous_ingest.py (*/10 min)                      │
│    → Deduped ingestion into RAG memory                       │
│                                                              │
│  Tier 2: periodic_analysis.py (4x daily: 7,11,15,19)         │
│    → Meeting prep, memory blocks, entity graph                │
│    → Phase 4: Contact reconciliation + CRM discovery          │
│                                                              │
│  Tier 3: intelligence_pipeline.py (daily 3:30 AM)             │
│    → Relationships, engagement, patterns, quality             │
│    → Phase 2.5: Contact enrichment (fill CRM fields)          │
│                                                              │
│  triage_prep.py (:14,:29,:44,:59) — 1 min before worker       │
│    → Extract pending items + enrich with DB contact context   │
└──────────────────────────────┬────────────────────────────────┘
                               │
                    enriched triage-inbox.json
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│         LAYER 2: TRIAGE WORKER (Kimi K2.5, */15 min)          │
│         Isolated session via OpenClaw cron                     │
│                                                              │
│  1. Read triage-inbox.json (NOT full log files)               │
│  2. Find entries needing processing                           │
│  3. Categorize: urgency, category, actionRequired             │
│  4. Handle routine: mark read, draft reply, dismiss           │
│  5. Escalate complex → worker-handoff.json                    │
│  6. Write triage-status.md (run summary)                      │
└──────────────────────────────┬────────────────────────────────┘
                               │
┌──────────────────────────────────────────────────────────────┐
│    LAYER 2.5: triage_cleanup.py (Python, 5 min after worker)  │
│    Mark processed items in logs, update heartbeat timestamp   │
└──────────────────────────────┬────────────────────────────────┘
                               │
┌──────────────────────────────────────────────────────────────┐
│    LAYER 3: supervisor_relay.py (Python, */10 min 6-23h)      │
│    • Meeting alerts within 20 min → directly to Telegram      │
│    • Stale worker / CRM health → handoff.json (not Telegram) │
│    • Cooldowns: stale 60 min, CRM 30 min                      │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  LAYER 3.5: SUPERVISOR (Kimi K2.5, hourly 7-22h, TELEGRAM)   │
│                                                              │
│  • Philip's sole gatekeeper — investigates before surfacing  │
│  • Reads triage-status.md + worker-handoff.json              │
│  • Surfaces concise alerts to Philip via Telegram             │
│  • HEARTBEAT_OK only when truly nothing to report             │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                     PHILIP (via Telegram)                      │
│                                                              │
│  Receives: escalation alerts, meeting reminders,              │
│            morning briefings, evening wind-downs,             │
│            vision alerts (unknown persons)                     │
└──────────────────────────────────────────────────────────────┘
```

## Log Entry Lifecycle

Every log entry tracks processing state via notifier fields:

```
null → categorize → act → pendingReviewAt → verify → reviewedAt
```

| Field | null means | timestamp means |
|-------|-----------|-----------------|
| categorizedAt | Needs processing | Triage worker categorized it |
| actionRequired | Needs decision | (string value — what to do) |
| actionCompletedAt | Action pending | Action was taken |
| pendingReviewAt | Not flagged | Awaiting verification |
| reviewedAt | Not verified | Entry complete |

## Escalation Lifecycle

```
worker creates → supervisor surfaces → supervisor resolves
```

| Field | null | timestamp |
|-------|------|-----------|
| surfacedAt | Not shown to Philip | Philip was notified |
| resolvedAt | Not yet acted on | Action completed |

## Memory Ingestion Flow

```
Content (any channel)
    │
    POST /ingest (port 9099)
    │
    ├─ fact_extraction.py → memory_facts (structured)
    ├─ entity_graph.py → memory_entities + memory_relations
    └─ rag.py → long_term_memory (embeddings)
```

Channels: discord, email, cli, api, telegram, camera, google_meet

## CRM Enrichment Flow

```
contact_identifiers (bridge table)
    │
    ├─ memory_entity_id ← periodic_analysis.py Phase 4 (fuzzy match, 4x daily)
    ├─ twenty_person_id ← contact discovery (create CRM records)
    └─ CRM fields ← intelligence_pipeline.py Phase 2.5 (daily)
         ├─ email domain → company (deterministic)
         └─ memory facts + transcripts → job title, city (LLM)
```

---

**Updated:** 2026-02-15
