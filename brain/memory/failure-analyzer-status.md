# Failure Analyzer Status

Last run: 2026-03-06 08:00 AM EST (13:00 UTC)
Failures analyzed: 22 (4 failed runs + 20 timeout runs reviewed)
Tasks created: 1
Classifications: transient: 18, config: 0, code: 1, unknown: 0

## New Task Created This Run

### ce4671ce — email-analyst: exec call hangs indefinitely on hook trigger
- **Agent:** email-analyst
- **Run ID:** 03b5e435-2a9b-4c7e-bf0f-3c10f8f08a08
- **Classification:** CODE
- **Root cause:** exec tool call at step 6 blocks indefinitely (>480s) on hook trigger downstream:email:triage.refreshed
- **Pattern:** Both email-analyst timeouts in 24h are on hook triggers; step 5 exec is fast (17ms), step 6 hangs
- **Assigned to:** overnight-pr
- **Priority:** normal

## Transient Failures (No Action Required)

### Infrastructure / Model Hangs (18 runs)
All zero-token, zero-step silent hangs — agents never reached LLM. Same pattern as the 18:00 EST mass event already tracked in task b4dca430.

| Agent | Runs | Pattern |
|-------|------|---------|
| main | 7 | 0-token zombie hangs, multiple triggers |
| email-classifier | 5 | 0-token hangs on hook + workflow triggers |
| calendar-monitor | 5 | 0-token hangs on cron + workflow triggers |
| email-analyst | 1 | 0-token zombie (43a3017f, 05:45 EST today) |

### Failed Runs (4 — all from 2026-02-27, outside 2h window)
- supervisor (2x): "All models failed to respond" + "list index out of range" — Feb 27, stale
- email-classifier (1): "All models failed to respond" — Feb 27, stale
- conversation-inbox (1): "All models failed to respond" — Feb 27, stale

## Existing Open Tasks (No Duplicates Created)

| Task ID | Title | Status |
|---------|-------|--------|
| d8a53d2b | calendar-monitor: Fix triage-inbox.json path | TODO |
| b4dca430 | Infrastructure: Mass timeout at 18:00 EST | TODO |
| a8191811 | conversation-inbox: 76.5% success rate | TODO |
| 4119a621 | Reduce email-classifier timeout 600s→360s | TODO |
| 927d5066 | calendar-monitor: Add max_iterations cap | TODO |
| bacf7570 | Increase failure-analyzer timeout 300s→480s | TODO |
| bd52e6c9 | improvement-analyst: Missing memory blocks | TODO |
| 1f78c6e4 | main:heartbeat 0 runs — broken schedule | TODO |
| 9246269a | Reduce conversation-resolver timeout | TODO |

## Fleet Health Snapshot (24h as of 08:00 EST)

| Agent | Runs | Success% | Timeouts | Avg Duration |
|-------|------|----------|----------|--------------|
| email-responder | 162 | 99.4% | 1 | 76s |
| email-classifier | 181 | 96.7% | 5 | 53s |
| email-analyst | 160 | 98.8% | 2 | 83s |
| main | 50 | 84.0% | 7 | 87s |
| calendar-monitor | 59 | 91.5% | 5 | 157s |
| conversation-inbox | 17 | 94.1% | 1 | 71s |
| vision-monitor | 3 | 66.7% | 1 | 66s |
| failure-analyzer | 10 | 90.0% | 0 | 190s |
| supervisor | 0 | — | — | — |

## Notes
- supervisor has 0 runs in 24h — all failures are from 2026-02-27 (stale, outside window)
- main's 14% timeout rate is inflated by the 18:00 EST infrastructure event (already tracked)
- failure-analyzer itself is healthy this run: 9/10 completed, 0 timeouts, avg 190s
- The bacf7570 timeout-increase task may no longer be needed given current 90% success rate
