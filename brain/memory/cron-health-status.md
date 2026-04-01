# Cron Health Status
Updated: 2026-04-01 00:00 ET

## Fleet Summary (24h)
- Runs: 158 (139 ok, 0 failed, 18 timeout)
- Failure rate: 11%
- Cost: $10.39
- Avg duration: 93s

**1 ERROR**, 0 stale, 12 healthy (13 total)

## Errors (7d)
- **crm-steward**: 0 failed, 4 timeout (57% fail rate), last: 14.0h ago

## Healthy Agents (7d)
| Agent | Runs | Failed | Avg Duration | Cost | Last Run |
|-------|------|--------|-------------|------|----------|
| calendar-monitor | 147 | 0 | 90s | $2.91 | 2.4h ago |
| chat-responder | 238 | 0 | 64s | $6.82 | 1.5h ago |
| conversation-inbox | 119 | 0 | 51s | $1.69 | 1.9h ago |
| conversation-resolver | 21 | 0 | 83s | $0.58 | 3.7h ago |
| email-analyst | 21 | 0 | 71s | $0.33 | 3.5h ago |
| email-classifier | 84 | 0 | 80s | $1.90 | 1.8h ago |
| email-responder | 49 | 0 | 94s | $2.24 | 3.8h ago |
| engine-report | 7 | 0 | 46s | $0.15 | 1.0h ago |
| evening-winddown | 7 | 0 | 77s | $3.07 | 3.0h ago |
| main | 233 | 0 | 111s | $8.32 | 2m ago |
| morning-briefing | 7 | 0 | 85s | $3.24 | 17.5h ago |
| vision-monitor | 21 | 0 | 105s | $0.33 | 5.8h ago |

## Tool Health (24h)
### Slowest Tools
- `spawn_agent`: avg 152s (3 calls)
- `web_search`: avg 780ms (55 calls)
- `web_fetch`: avg 746ms (4 calls)
### Most-Failing Tools
- `exec`: 29/567 failed (5.1%)
- `browser`: 13/92 failed (14.1%)
- `web_fetch`: 11/15 failed (73.3%)

