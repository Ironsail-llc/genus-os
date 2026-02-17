# Cron Map — Unified Schedule

All times in America/New_York (ET). System timezone is EST (UTC-5), Philip is AST (UTC-4).

## Timeline

```
Every 5 min    │ Calendar sync (crontab) — brain/scripts/calendar_sync.py
               │ Email sync (crontab) — brain/scripts/email_sync.py

Every 10 min   │ Continuous ingestion (crontab, Tier 1) — brain/memory_system/continuous_ingest.py
               │ Meet transcript sync (crontab) — brain/scripts/meet_transcript_sync.py
               │ Supervisor relay (crontab, 6-23h) — brain/scripts/supervisor_relay.py
               │ Vision Monitor (OpenClaw, 7-23h) — check motion events, alert visitors

Every 15 min   │ Garmin health sync (crontab) — health/garmin_sync.py
               │ Triage Worker (OpenClaw) — process triage-inbox, categorize, act, escalate

:14,:29,:44,:59│ Triage prep (crontab) — brain/scripts/triage_prep.py
               │   Extract pending items + enrich with DB contact context

:05,:20,:35,:50│ Triage cleanup (crontab) — brain/scripts/triage_cleanup.py
               │   Mark processed items in logs, update heartbeat

Every 30 min   │ Jira sync (crontab, 6-22h M-F) — brain/scripts/jira_sync.py
  (M-F only)   │

Hourly         │ Supervisor Heartbeat (OpenClaw, 7-22h, on Telegram) — investigate + surface
               │ System health check (crontab) — brain/scripts/system_health_check.py

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

06:30 AM       │ Morning Briefing (OpenClaw) — calendar, email, weather, news → Telegram

07:00, 11:00,  │ Periodic analysis (crontab, Tier 2) — brain/memory_system/periodic_analysis.py
15:00, 19:00   │   Phase 1: Meeting prep briefs
               │   Phase 2: Memory block updates
               │   Phase 3: Entity graph enrichment
               │   Phase 4: Contact reconciliation + CRM discovery

21:00          │ Evening Wind-Down (OpenClaw) — tomorrow preview, open items → Telegram

Sunday 04:00   │ Data archival (crontab) — brain/scripts/data_archival.py

04:30 AM       │ SSD backup (crontab, daily) — ~/robothor/scripts/backup-ssd.sh
               │   rsync all dirs + 3x pg_dump + Docker volumes + credentials + manifest

Sunday 05:00   │ Weekly review (crontab) — brain/memory_system/weekly_review.py
               │   Deep synthesis → memory/weekly-review-YYYY-MM-DD.md
```

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

# Triage Prep - extract pending items 1 min before triage worker
14,29,44,59 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/triage_prep.py >> memory_system/logs/triage-prep.log 2>&1

# Triage Cleanup - mark processed items 5 min after worker
5,20,35,50 * * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/triage_cleanup.py >> memory_system/logs/triage-cleanup.log 2>&1

# Supervisor Relay - meeting alerts + stale/CRM checks (6-23 EST = 7-00 AST)
*/10 6-23 * * * cd /home/philip/clawd && /home/philip/clawd/memory_system/venv/bin/python scripts/supervisor_relay.py >> memory_system/logs/supervisor-relay.log 2>&1
```

## OpenClaw Cron Jobs (runtime/cron/jobs.json)

| ID | Name | Schedule | Session | Delivery |
|----|------|----------|---------|----------|
| a1b2...0001 | Triage Worker | */15 * * * * | isolated | announce |
| b7e3...0001 | Supervisor Heartbeat | 0 7-22 * * * | isolated | announce → telegram |
| vision...0001 | Vision Monitor | */10 7-23 * * * | isolated | announce → telegram |
| 282b...b829 | Morning Briefing | 30 6 * * * | isolated | announce → telegram |
| 88db...dca0 | Evening Wind-Down | 0 21 * * * | isolated | announce → telegram |

### One-Shot Jobs (deleteAfterRun)

| Name | Scheduled | Status |
|------|-----------|--------|
| Reminder: Ask Dad to Feed the Eel | 2026-02-21 14:00 UTC | enabled |

## Notes

- All OpenClaw crons use **Kimi K2.5** (via OpenRouter). Opus 4.6 is first fallback.
- Triage Worker runs silently — output suppressed unless escalation needed
- Supervisor Heartbeat outputs to Telegram but suppresses HEARTBEAT_OK
- Vision Monitor only alerts on noteworthy events (visitors, deliveries, anomalies)
- Morning Briefing and Evening Wind-Down always deliver to Telegram
- Triage Prep runs 1 min before worker, Triage Cleanup runs 5 min after
- Supervisor Relay is Python (not LLM) — handles meeting alerts and stale/CRM checks
- System timezone is EST (UTC-5), Philip is AST (UTC-4) — 1 hour offset

---

**Updated:** 2026-02-16
