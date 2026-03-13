# Failure Analyzer Status

Last run: 2026-03-13 06:25 UTC (2026-03-13 02:25 AM EDT)
Failures analyzed: window 04:25–06:25 UTC (00:25–02:25 AM EDT on 2026-03-13)
Tasks created: 0
Classifications: transient: 0, config: 0, code: 0, unknown: 0

## Run Summary

### Analysis Window
- 2026-03-13 04:25 UTC – 2026-03-13 06:25 UTC (00:25–02:25 AM EDT)

### Failed Runs
- All `failed` status runs fleet-wide are from 2026-03-08/09 (>48h stale — skipped per rules)

### Timeout Runs in Window
- **No new timeout runs detected in this 2h window** (04:25–06:25 UTC)
- Most recent timeout was chat-responder `c70fd4eb` started 2026-03-12 22:30 UTC (18:30 EDT) — prior window, already tracked in task 60d7b991
- Most recent timeout batch was 2026-03-12 22:00 UTC (18:00 EDT) — 6-agent wave: main, calendar-monitor (×2), email-responder, conversation-inbox, chat-responder

### Fleet Health (24h stats as of run)
| Agent           | Runs | OK  | Timeouts | Rate   | Status   |
|-----------------|------|-----|----------|--------|----------|
| main            | 21   | 14  | 7        | 33.3%  | 🚨 CRITICAL |
| email-classifier| 12   | 8   | 4        | 33.3%  | 🚨 CRITICAL |
| chat-responder  | 35   | 30  | 5        | 14.3%  | 🚨 HIGH   |
| calendar-monitor| 25   | 20  | 5        | 20.0%  | 🚨 HIGH   |
| email-responder | 2    | 1   | 1        | 50.0%  | ⚠️ WATCH  |
| conversation-inbox | 5 | 4   | 1        | 20.0%  | ⚠️ WATCH  |
| failure-analyzer| 12   | 9   | 2        | 16.7%  | ⚠️ WATCH  |

### Overnight Status
- Fleet currently in quiet overnight period (02:00–06:00 UTC) — low cron activity
- Last 5 runs for main, email-classifier, chat-responder: ALL COMPLETED SUCCESSFULLY
- Zombie `c70fd4eb` (chat-responder) still running with null completed_at (8h+ duration) — zombie cleanup still not operational

### Existing Open Tasks (no duplicates created)
- `f494392a` — Philip escalation: PRs + write_path guardrail blocker (URGENT, needs-philip)
- `60d7b991` — Zombie infrastructure crisis (URGENT, code+infrastructure) — updated this run
- `08d64e1a` — z-ai/glm-5-20260211 second-call LLM hang (HIGH, config+infrastructure)
- `a25f12b5` — email-responder exec_allowlist block (HIGH, config)
- `fc1d23bb` — email-responder timeout 600→720s (HIGH, config)
- `31f31fc9` — failure-analyzer context-bloat LLM hang (HIGH, code+config)
- `9412c7fd` — conversation-inbox timeout correction PR#19 (HIGH, config)
- `d66659bc` — chat-responder LLM hang moonshotai model (NORMAL, code+config)
- `43e41fed` — calendar-monitor leading-space tool name bug (HIGH, code)
- `c51a8374` — email-analyst zombie cron pattern (HIGH, code+infrastructure)
- `5891440c` — email-responder zombie hook pattern (HIGH, code+infrastructure)
- `740a4a86` — email-analyst hook zombie (HIGH, code)
- `ce4671ce` — email-analyst exec hang (NORMAL, code)

### Root Cause Summary (Ongoing)
1. **PRIMARY: Zombie runtime crisis** — 16 mass zombie events over 4+ days, cleanup not running
2. **SECONDARY: z-ai/glm-5-20260211 LLM hang** — second-call hang pattern across main, calendar-monitor, email-responder
3. **BLOCKER: write_path guardrail** — overnight-pr cannot apply any config fixes (9 PRs pending, DAY 9 unmerged)
4. **PENDING PHILIP ACTION** — see task f494392a for ordered action list

### No New Tasks Created This Run
Reason: All timeout events in the current window (04:25–06:25 UTC) are either:
- Prior to the window (stale, already tracked in existing tasks)
- Within existing task scope (zombie crisis, z-ai model, write_path blocker)
Creating duplicate tasks is explicitly prohibited by operating rules.
