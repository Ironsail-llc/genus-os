# Cron Map — Unified Schedule

All times in America/New_York (ET). System timezone is EST (UTC-5), Philip is AST (UTC-4).

## Timeline

```
Every 5 min    │ Calendar sync (crontab) — brain/scripts/calendar_sync.py
               │ Email sync (crontab) — brain/scripts/email_sync.py

Every 10 min   │ Continuous ingestion (crontab, Tier 1) — brain/memory_system/continuous_ingest.py
               │ Meet transcript sync (crontab) — brain/scripts/meet_transcript_sync.py
               │ Supervisor relay (crontab, 6-23h) — brain/scripts/supervisor_relay.py

Every 15 min   │ Garmin health sync (crontab) — health/garmin_sync.py

05:15, 19:45   │ Health summary (crontab) — brain/scripts/health_summary.py
               │   Reads garmin.db → writes memory/garmin-health.md (before briefing agents)

Every 30 min   │ Jira sync (crontab, 6-22h M-F) — brain/scripts/jira_sync.py
               │ Cron health check (crontab) — brain/scripts/cron_health_check.py

Hourly         │ Email Classifier (OpenClaw, every 2h 6-22, announce) — classify emails, route or escalate
               │ Calendar Monitor (OpenClaw, every 2h 6-22, announce) — detect conflicts, cancellations, changes
               │ Supervisor Heartbeat (OpenClaw, every 2h 6-22, → Telegram) — reads all status files, surfaces changes
               │ Vision Monitor (OpenClaw, hourly 24/7, silent) — check motion events, write status file
               │ Conversation Inbox Monitor (OpenClaw, hourly 6-22, silent) — check urgent messages, write status file
               │ System health check (crontab) — brain/scripts/system_health_check.py
:10            │ Triage cleanup (crontab) — brain/scripts/triage_cleanup.py
               │   Mark processed items in logs, update heartbeat
:20            │ Email analysis cleanup (crontab) — clear stale response-analysis.json
:25            │ Email response prep (crontab) — brain/scripts/email_response_prep.py
               │   Enrich queued emails with thread + contact + topic RAG + calendar + CRM history + depth tag
:30            │ Email Analyst (OpenClaw, every 2h 8-20, announce) — analyze analytical items
:55            │ Triage prep (crontab) — brain/scripts/triage_prep.py
               │   Extract pending items + enrich with DB contact context (prepares for next hour)

03:00 AM       │ Memory maintenance (crontab) — brain/memory_system/maintenance.sh
               │   TTL expiry, archival, stats

03:15 AM       │ CRM consistency (crontab) — brain/scripts/crm_consistency.py
               │   Cross-system contact/entity checks

03:30 AM       │ Intelligence pipeline (crontab, Tier 3) — brain/memory_system/intelligence_pipeline.py
               │   Phase 1: Catch-up ingestion
               │   Phase 2: Relationship intelligence
               │   Phase 2.5: Contact enrichment (fill CRM fields from evidence)
               │   Phase 3-6: Engagement, patterns, quality, cleanup

04:00 AM       │ Snapshot cleanup (crontab) — delete vision snapshots > 30 days

Every 4h 6-22  │ Task Cleanup (crontab) — brain/scripts/task_cleanup.py
               │   Delete test data, resolve past-date calendar tasks, reset stuck IN_PROGRESS, resolve orphan TODOs

Every 4h 8-20  │ Email Responder (OpenClaw, announce) — compose and send replies (substantive for analytical)

8, 14, 20      │ Conversation Resolver (OpenClaw, silent) — auto-resolve stale conversations (>7d inactive)

06:30 AM       │ Morning Briefing (OpenClaw) — calendar, email, weather, news → Telegram

07:00, 11:00,  │ Periodic analysis (crontab, Tier 2) — brain/memory_system/periodic_analysis.py
15:00, 19:00   │   Phase 1: Meeting prep briefs
               │   Phase 2: Memory block updates
               │   Phase 3: Entity graph enrichment
               │   Phase 4: Contact reconciliation + CRM discovery

10:00          │ CRM Steward (OpenClaw, daily) — data hygiene + contact enrichment via sub-agents

21:00          │ Evening Wind-Down (OpenClaw) — tomorrow preview, open items → Telegram

Sunday 04:00   │ Data archival (crontab) — brain/scripts/data_archival.py

04:30 AM       │ SSD backup (crontab, daily) — ~/robothor/scripts/backup-ssd.sh
               │   rsync all dirs + 3x pg_dump + Docker volumes + credentials + manifest

Sunday 05:00   │ Weekly review (crontab) — brain/memory_system/weekly_review.py
               │   Deep synthesis → memory/weekly-review-YYYY-MM-DD.md
```

## Credential Injection

All cron jobs that need credentials are wrapped with `cron-wrapper.sh`:
```
W=/home/philip/robothor/scripts/cron-wrapper.sh
*/5 * * * * cd /home/philip/clawd && $W <python command>
```
The wrapper sources `/run/robothor/secrets.env` (SOPS-decrypted at boot) before executing. Jobs that don't need credentials (find cleanup, backup) run without the wrapper.

## System Crontab (crontab -l)

```crontab
# Calendar Sync - every 5 min
*/5 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/calendar_sync.py >> memory_system/logs/calendar-sync.log 2>&1

# Email Sync - every 5 min
*/5 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/email_sync.py >> memory_system/logs/email-sync.log 2>&1

# Jira Sync - every 30 min during work hours (M-F)
*/30 6-22 * * 1-5 cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/jira_sync.py >> memory_system/logs/jira-sync.log 2>&1

# Garmin health sync - every 15 min
*/15 * * * * /home/philip/garmin-sync/venv/bin/python /home/philip/garmin-sync/garmin_sync.py >> /home/philip/garmin-sync/sync.log 2>&1

# Health Summary — garmin-health.md for briefing agents (before 6:30 AST briefing + 21:00 AST winddown)
15 5 * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/health_summary.py >> memory_system/logs/health-summary.log 2>&1
45 19 * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/health_summary.py >> memory_system/logs/health-summary.log 2>&1

# Google Meet Transcript Sync - every 10 min
*/10 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/meet_transcript_sync.py >> memory_system/logs/meet-transcript-sync.log 2>&1

# Intelligence Pipeline — Three Tiers
# Tier 1: Continuous ingestion (every 10 min, deduped, ~10 min freshness)
*/10 * * * * cd /home/philip/clawd/memory_system && ./venv/bin/python continuous_ingest.py >> logs/continuous-ingest.log 2>&1

# Tier 2: Periodic analysis (4x daily — meeting prep, blocks, entities, contact reconciliation)
0 7,11,15,19 * * * cd /home/philip/clawd/memory_system && ./venv/bin/python periodic_analysis.py >> logs/periodic-analysis.log 2>&1

# Tier 3: Deep analysis (daily 3:30 AM — relationships, enrichment, engagement, patterns, quality)
30 3 * * * cd /home/philip/clawd/memory_system && ./venv/bin/python intelligence_pipeline.py >> logs/intelligence.log 2>&1

# Memory maintenance (3 AM) - TTL expiry, archival
0 3 * * * /home/philip/clawd/memory_system/maintenance.sh

# CRM Consistency Check - daily at 3:15 AM
15 3 * * * cd /home/philip/clawd/memory_system && ./venv/bin/python /home/philip/clawd/scripts/crm_consistency.py >> logs/crm-consistency.log 2>&1

# Snapshot cleanup - delete vision snapshots > 30 days
0 4 * * * find /home/philip/clawd/memory/snapshots -name '*.jpg' -mtime +30 -delete && find /home/philip/clawd/memory/snapshots -type d -empty -delete

# Data Archival - Sunday at 4:00 AM
0 4 * * 0 cd /home/philip/clawd/memory_system && ./venv/bin/python /home/philip/clawd/scripts/data_archival.py >> logs/data-archival.log 2>&1

# Daily SSD backup - 4:30 AM
30 4 * * * /home/philip/robothor/scripts/backup-ssd.sh >> /home/philip/robothor/scripts/backup.log 2>&1

# Weekly Deep Review - Sunday at 5:00 AM
0 5 * * 0 cd /home/philip/clawd/memory_system && ./venv/bin/python weekly_review.py >> logs/weekly-review.log 2>&1

# System Health Check - hourly
0 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/system_health_check.py >> memory_system/logs/health-check.log 2>&1

# Cron Agent Health Check - every 30 min
*/30 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/cron_health_check.py >> memory_system/logs/cron-health-check.log 2>&1

# Triage Prep - extract pending items (hourly, prepares for next hour's Classifier)
55 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/triage_prep.py >> memory_system/logs/triage-prep.log 2>&1

# Triage Cleanup - mark processed items (hourly, 10 min after Classifier)
10 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/triage_cleanup.py >> memory_system/logs/triage-cleanup.log 2>&1

# Supervisor Relay - meeting alerts + stale/CRM checks (6-23 EST = 7-00 AST)
*/10 6-23 * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/supervisor_relay.py >> memory_system/logs/supervisor-relay.log 2>&1

# Email Analysis Cleanup - clear stale analysis before enrichment (hourly :20)
20 * * * * /home/philip/clawd/memory_system/venv/bin/python3 -c "import json,os; p=os.path.expanduser('~/clawd/memory/response-analysis.json'); open(p,'w').write(json.dumps({'analyses':{}}))" 2>/dev/null
```

## OpenClaw Cron Jobs (runtime/cron/jobs.json)

| ID | Name | Schedule | Session | Delivery |
|----|------|----------|---------|----------|
| email-classifier-0001 | Email Classifier | 0 6-22/2 * * * | isolated | announce → telegram |
| calendar-monitor-0001 | Calendar Monitor | 0 6-22/2 * * * | isolated | announce → telegram |
| email-analyst-0001 | Email Analyst | 30 8-20/2 * * * | isolated | announce → telegram |
| email-responder-0001 | Email Responder | 0 8-20/4 * * * | isolated | announce → telegram |
| b7e3...0001 | Supervisor Heartbeat | 0 6-22/2 * * * | isolated | announce → telegram |
| vision...0001 | Vision Monitor | 0 * * * * | isolated | none (silent) |
| conversation-inbox-monitor-0001 | Conversation Inbox Monitor | 0 6-22 * * * | isolated | none (silent) |
| conversation-resolver-0001 | Conversation Resolver | 0 8,14,20 * * * | isolated | none (silent) |
| crm-steward-0001 | CRM Steward | 0 10 * * * | isolated | announce → telegram |
| 282b...b829 | Morning Briefing | 30 6 * * * | isolated | announce → telegram |
| 88db...dca0 | Evening Wind-Down | 0 21 * * * | isolated | announce → telegram |

### Retired Jobs (disabled)

| ID | Name | Replaced By |
|----|------|-------------|
| a1b2...0001 | Triage Worker | Email Classifier + Calendar Monitor |

### One-Shot Jobs (deleteAfterRun)

| Name | Scheduled | Status |
|------|-----------|--------|
| Reminder: Ask Dad to Feed the Eel | 2026-02-21 14:00 UTC | enabled |
| Merrimack Loan Payment Reminder | 2026-03-02 14:00 UTC | disabled (moved to CRM tasks) |
| Merrimack Follow-up Reminder | 2026-02-20 14:00 UTC | disabled (moved to CRM tasks) |

## Notes

- All OpenClaw crons use **Kimi K2.5** (via OpenRouter). Sonnet 4.6 is first fallback.
- Pipeline agents use `delivery: announce` — output delivered to Telegram. Background agents (Vision, Conversation Inbox, Conversation Resolver) use `delivery: none` (agents still run, output just isn't delivered)
- Supervisor runs every 2 hours and reads all worker status files + cron-health-status.md + worker-handoff.json
- Workers output `HEARTBEAT_OK` when nothing to report (suppressed by framework)
- Main session heartbeat has `activeHours: 06:00-22:00 AST` — no wakeups during quiet hours (10 PM - 6 AM)
- Hook-based email pipeline is primary (~60s email-to-reply). Crons are hourly safety net.
- Hourly email timeline: :10 cleanup → :20 analysis reset → :25 enrichment → :30 Analyst → :55 triage prep (Classifier/Responder on their own 2h/4h schedules)
- Duplicate prevention: filter_already_replied() in response prep, actionCompletedAt guard in cleanup, 5-min cooldown in sync
- Supervisor Relay is Python (not LLM) — handles meeting alerts and stale/CRM checks
- CRM Steward spawns research sub-agents for contact enrichment (max 3 per run)
- System timezone is EST (UTC-5), Philip is AST (UTC-4) — 1 hour offset

---

**Updated:** 2026-02-27
