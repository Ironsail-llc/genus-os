# Cron Health Status
Updated: 2026-03-21 21:00 ET

## Fleet Summary (24h)
- Runs: 158 (151 ok, 0 failed, 5 timeout)
- Failure rate: 3%
- Cost: $2.52
- Avg duration: 89s

**1 ERROR**, 0 stale, 16 healthy (17 total)

## Errors (7d)
- **canary**: 0 failed, 1 timeout (100% fail rate), last: 40m ago

## Healthy Agents (7d)
| Agent | Runs | Failed | Avg Duration | Cost | Last Run |
|-------|------|--------|-------------|------|----------|
| calendar-monitor | 169 | 0 | 100s | $2.29 | 2.9h ago |
| chat-responder | 238 | 0 | 61s | $1.49 | 27m ago |
| conversation-inbox | 119 | 0 | 56s | $1.23 | 55m ago |
| conversation-resolver | 21 | 0 | 134s | $0.41 | 40m ago |
| crm-steward | 7 | 0 | 216s | $0.34 | 11.0h ago |
| email-analyst | 21 | 0 | 84s | $0.17 | 30m ago |
| email-classifier | 84 | 0 | 67s | $0.93 | 50m ago |
| email-responder | 49 | 0 | 82s | $0.60 | 45m ago |
| engine-report | 7 | 0 | 77s | $0.08 | 22.0h ago |
| evening-winddown | 7 | 0 | 113s | $1.20 | 1s ago |
| failure-analyzer | 35 | 0 | 116s | $0 | 4.1d ago |
| improvement-analyst | 6 | 0 | 284s | $0 | 4.8d ago |
| main | 254 | 0 | 102s | $3.73 | 1s ago |
| morning-briefing | 7 | 0 | 89s | $0.50 | 14.5h ago |
| overnight-pr | 3 | 0 | 395s | $0 | 4.8d ago |
| vision-monitor | 21 | 0 | 114s | $0.16 | 2.8h ago |

## Tool Health (24h)
### Slowest Tools
- `look`: avg 9s (3 calls)
- `web_search`: avg 1s (28 calls)
- `gws_calendar_list`: avg 886ms (10 calls)
### Most-Failing Tools
- `exec`: 24/363 failed (6.6%)
- `web_fetch`: 5/20 failed (25.0%)
- `write_file`: 4/51 failed (7.8%)

