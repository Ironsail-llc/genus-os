# Cron Map — Unified Schedule

All times in America/New_York (ET).

## Timeline

```
Every 5 min    │ Calendar sync (crontab) — brain/scripts/calendar_sync.py
               │ Email sync (crontab) — brain/scripts/email_sync.py

Every 10 min   │ Continuous ingestion (crontab, Tier 1) — brain/memory_system/continuous_ingest.py
               │ Meet transcript sync (crontab) — brain/scripts/meet_transcript_sync.py
               │ Supervisor relay (crontab, 6-23h) — brain/scripts/supervisor_relay.py

Every 15 min   │ Garmin health sync (crontab) — robothor.health.sync

05:15, 19:45   │ Health summary (crontab) — robothor.health.summary
               │   Reads PostgreSQL → writes memory/garmin-health.md (before briefing agents)

Every 30 min   │ Jira sync (crontab, 6-22h M-F) — brain/scripts/jira_sync.py
               │ Cron health check (crontab) — brain/scripts/cron_health_check.py

Hourly         │ Email Classifier (Engine, 2h safety net 6-22, silent, primary: hook email.new + triage.refreshed) — classify emails, route or escalate
               │ Calendar Monitor (Engine, 6h safety net 6-22, silent, primary: hook calendar.*) — detect conflicts, cancellations, changes
               │ Main Heartbeat (Engine, hourly 6-22, → Telegram) — reads all status files, surfaces decisions
               │ Vision Monitor (Engine, 6h safety net 6-22, silent, primary: hook vision.person_unknown) — check motion events, write status file
               │ Conversation Inbox Monitor (Engine, hourly 6-22, silent) — check urgent messages, write status file
               │ System health check (crontab) — brain/scripts/system_health_check.py
:20            │ Email analysis cleanup (crontab) — clear stale response-analysis.json
:30            │ Email Analyst (Engine, 6h safety net 8-20, primary: downstream from classifier) — analyze analytical items

03:00 AM       │ Memory maintenance (crontab) — brain/memory_system/maintenance.sh
               │   TTL expiry, archival, stats
03:00 AM       │ Nightwatch Heal (crontab) — brain/scripts/nightwatch-heal.py
               │   Fixes tracked failures via Claude Code in git worktrees
               │   Creates draft PRs for review (max 3/night)

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

Every 2h 8-20  │ Email Responder (Engine, silent) — compose and send replies (substantive for analytical)

8, 14, 20      │ Conversation Resolver (Engine, silent) — auto-resolve stale conversations (>7d inactive)

06:30 AM       │ Morning Briefing (Engine) — calendar, email, weather, news → Telegram

07:00, 11:00,  │ Periodic analysis (crontab, Tier 2) — brain/memory_system/periodic_analysis.py
15:00, 19:00   │   Phase 1: Meeting prep briefs
               │   Phase 2: Memory block updates
               │   Phase 3: Entity graph enrichment
               │   Phase 4: Contact reconciliation + CRM discovery

10:00          │ CRM Steward (Engine, daily) — data hygiene + contact enrichment via sub-agents

21:00          │ Evening Wind-Down (Engine) — tomorrow preview, open items → Telegram

Sunday 01:00   │ Nightwatch Research (crontab) — brain/scripts/nightwatch-research.py
               │   Competitive analysis of agent frameworks via Claude Code + web search
               │   Creates feature tasks tagged nightwatch+feature

Monday 03:00   │ Nightwatch Build (crontab) — brain/scripts/nightwatch-build.py
               │   Implements features from research via Claude Code in git worktrees
               │   Creates draft PRs for review

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
*/5 * * * * cd /home/philip/robothor/brain && $W <python command>
```
The wrapper sources `/run/robothor/secrets.env` (SOPS-decrypted at boot) before executing. Jobs that don't need credentials (find cleanup, backup) run without the wrapper.

## System Crontab (crontab -l)

```crontab
# Calendar Sync - every 5 min
*/5 * * * * cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/calendar_sync.py >> memory_system/logs/calendar-sync.log 2>&1

# Email Sync - every 5 min
*/5 * * * * cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/email_sync.py >> memory_system/logs/email-sync.log 2>&1

# Jira Sync - every 30 min during work hours (M-F)
*/30 6-22 * * 1-5 cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/jira_sync.py >> memory_system/logs/jira-sync.log 2>&1

# Garmin health sync - every 15 min (PostgreSQL via robothor.health.sync)
*/15 * * * * cd /home/philip/robothor/brain && ROBOTHOR_DB_USER=philip /home/philip/robothor/brain/memory_system/venv/bin/python -m robothor.health.sync >> memory_system/logs/garmin-sync.log 2>&1

# Health Summary — garmin-health.md for briefing agents (before 6:30 ET briefing + 21:00 ET winddown)
15 5 * * * cd /home/philip/robothor/brain && ROBOTHOR_DB_USER=philip /home/philip/robothor/brain/memory_system/venv/bin/python -m robothor.health.summary >> memory_system/logs/health-summary.log 2>&1
45 19 * * * cd /home/philip/robothor/brain && ROBOTHOR_DB_USER=philip /home/philip/robothor/brain/memory_system/venv/bin/python -m robothor.health.summary >> memory_system/logs/health-summary.log 2>&1

# Google Meet Transcript Sync - every 10 min
*/10 * * * * cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/meet_transcript_sync.py >> memory_system/logs/meet-transcript-sync.log 2>&1

# Intelligence Pipeline — Three Tiers
# Tier 1: Continuous ingestion (every 10 min, deduped, ~10 min freshness)
*/10 * * * * cd /home/philip/robothor/brain/memory_system && ./venv/bin/python continuous_ingest.py >> logs/continuous-ingest.log 2>&1

# Tier 2: Periodic analysis (4x daily — meeting prep, blocks, entities, contact reconciliation)
0 7,11,15,19 * * * cd /home/philip/robothor/brain/memory_system && ./venv/bin/python periodic_analysis.py >> logs/periodic-analysis.log 2>&1

# Tier 3: Deep analysis (daily 3:30 AM — relationships, enrichment, engagement, patterns, quality)
30 3 * * * cd /home/philip/robothor/brain/memory_system && ./venv/bin/python intelligence_pipeline.py >> logs/intelligence.log 2>&1

# Memory maintenance (3 AM) - TTL expiry, archival
0 3 * * * /home/philip/robothor/brain/memory_system/maintenance.sh

# CRM Consistency Check - daily at 3:15 AM
15 3 * * * cd /home/philip/robothor/brain/memory_system && ./venv/bin/python /home/philip/robothor/brain/scripts/crm_consistency.py >> logs/crm-consistency.log 2>&1

# Snapshot cleanup - delete vision snapshots > 30 days
0 4 * * * find /home/philip/robothor/brain/memory/snapshots -name '*.jpg' -mtime +30 -delete && find /home/philip/robothor/brain/memory/snapshots -type d -empty -delete

# Data Archival - Sunday at 4:00 AM
0 4 * * 0 cd /home/philip/robothor/brain/memory_system && ./venv/bin/python /home/philip/robothor/brain/scripts/data_archival.py >> logs/data-archival.log 2>&1

# Daily SSD backup - 4:30 AM
30 4 * * * /home/philip/robothor/scripts/backup-ssd.sh >> /home/philip/robothor/scripts/backup.log 2>&1

# Weekly Deep Review - Sunday at 5:00 AM
0 5 * * 0 cd /home/philip/robothor/brain/memory_system && ./venv/bin/python weekly_review.py >> logs/weekly-review.log 2>&1

# System Health Check - hourly
0 * * * * cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/system_health_check.py >> memory_system/logs/health-check.log 2>&1

# Cron Agent Health Check - every 30 min
*/30 * * * * cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/cron_health_check.py >> memory_system/logs/cron-health-check.log 2>&1

# Supervisor Relay - meeting alerts + stale/CRM checks (6-23 ET)
*/10 6-23 * * * cd /home/philip/robothor/brain && /home/philip/robothor/brain/memory_system/venv/bin/python scripts/supervisor_relay.py >> memory_system/logs/supervisor-relay.log 2>&1

# Nightwatch Self-Healing - fix bugs nightly at 3 AM
0 3 * * * /home/philip/robothor/brain/scripts/nightwatch-cron.sh nightwatch-heal.py

# Nightwatch Research - competitive analysis weekly (Sunday 1 AM)
0 1 * * 0 /home/philip/robothor/brain/scripts/nightwatch-cron.sh nightwatch-research.py

# Nightwatch Build - implement features weekly (Monday 3 AM)
0 3 * * 1 /home/philip/robothor/brain/scripts/nightwatch-cron.sh nightwatch-build.py

# Email Analysis Cleanup - clear stale analysis before enrichment (hourly :20)
20 * * * * /home/philip/robothor/brain/memory_system/venv/bin/python3 -c "import json,os; p=os.path.expanduser('~/robothor/brain/memory/response-analysis.json'); open(p,'w').write(json.dumps({'analyses':{}}))" 2>/dev/null
```

## Engine Agent Crons (APScheduler from `docs/agents/*.yaml`)

| Agent ID | Schedule | Model | Delivery | Primary Trigger |
|----------|----------|-------|----------|----------------|
| email-classifier | `0 6-22/2 * * *` | Kimi K2.5 | none (silent) | hook: email.new, triage.refreshed |
| calendar-monitor | `0 6-22/6 * * *` | Kimi K2.5 | none (silent) | hook: calendar.* |
| email-analyst | `30 8-20/6 * * *` | Kimi K2.5 | none (silent) | downstream from classifier |
| email-responder | `0 8-20/2 * * *` | Sonnet 4.6 | none (silent) | downstream from classifier |
| main:heartbeat | `0 6-22 * * *` | Sonnet 4.6 | announce → Telegram | cron |
| vision-monitor | `0 6-22/6 * * *` | Kimi K2.5 | none (silent) | hook: vision.person_unknown |
| conversation-inbox | `0 6-22 * * *` | Kimi K2.5 | none (silent) | cron |
| conversation-resolver | `0 8,14,20 * * *` | Kimi K2.5 | none (silent) | cron |
| crm-steward | `0 10 * * *` | Kimi K2.5 | none (silent) | cron |
| morning-briefing | `30 6 * * *` | Kimi K2.5 | announce → Telegram | cron |
| evening-winddown | `0 21 * * *` | Kimi K2.5 | announce → Telegram | cron |
| failure-analyzer | `0 */2 * * *` | Sonnet 4.6 | none (silent) | cron |
| improvement-analyst | `0 2 * * *` | Sonnet 4.6 | none (silent) | cron (via nightwatch workflow) |
| overnight-pr | `0 3 * * *` | Sonnet 4.6 | none (silent) | cron (via nightwatch workflow) |

## Engine Workflow Crons (APScheduler from `docs/workflows/*.yaml`)

| Workflow ID | Schedule | Steps | Primary Trigger |
|-------------|----------|-------|----------------|
| email-pipeline | `0 6-22/6 * * *` | classify → condition → analyze/respond | hook: email.new |
| calendar-pipeline | `0 6-22/6 * * *` | monitor → done | hook: calendar.* |
| nightwatch | `0 2 * * *` | analyst → condition → overnight-pr | cron (nightly) |

`vision-pipeline` is hook-only (no cron).

## Notes

- All Engine agents use **Kimi K2.5** except Email Responder (**Sonnet 4.6**, quality-critical) and all Nightwatch agents (**Sonnet 4.6**).
- Only 3 agents talk to the owner: Main heartbeat (decisions), Morning Briefing (daily), Evening Wind-Down (daily). All worker agents are silent — they coordinate via tasks, status files, and notification inbox.
- Main heartbeat runs hourly and reads all worker status files. Always sends a report — never silent.
- Workers write status files and stop silently.
- Main session has `activeHours: 06:00-22:00 ET` — no wakeups during quiet hours (10 PM - 6 AM).
- **Event-driven hooks are the primary trigger** for email, calendar, and vision agents. Crons are 6h safety nets.
- **Declarative workflow engine** (`robothor/engine/workflow.py`) provides multi-step agent pipelines with conditional routing. Workflows are defined in `docs/workflows/*.yaml`.
- Hourly email timeline: :20 analysis reset → :30 Analyst
- Email classifier now self-manages `categorizedAt` in email-log.json
- Duplicate prevention: actionCompletedAt guard, 5-min cooldown in sync
- Supervisor Relay is Python (not LLM) — handles meeting alerts and stale/CRM checks
- CRM Steward spawns research sub-agents for contact enrichment (max 3 per run)
- System timezone is America/New_York (ET)

---

**Updated:** 2026-03-05
