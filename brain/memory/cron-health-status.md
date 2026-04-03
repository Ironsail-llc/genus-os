# Cron Health Status
Updated: 2026-04-03 06:30 ET

## Fleet Summary (24h)
- Runs: 168 (164 ok, 0 failed, 3 timeout)
- Failure rate: 2%
- Cost: $11.17
- Avg duration: 61s

**1 ERROR**, 0 stale, 13 healthy (14 total)

## Errors (7d)
- **crm-steward**: 0 failed, 5 timeout (83% fail rate), last: 1.9d ago

## Healthy Agents (7d)
| Agent | Runs | Failed | Avg Duration | Cost | Last Run |
|-------|------|--------|-------------|------|----------|
| calendar-monitor | 209 | 0 | 81s | $4.00 | 22m ago |
| canary | 1 | 0 | 7s | $0 | 14.6h ago |
| chat-responder | 238 | 0 | 61s | $17.78 | 27m ago |
| conversation-inbox | 119 | 0 | 54s | $1.66 | 25m ago |
| conversation-resolver | 21 | 0 | 82s | $0.52 | 10.2h ago |
| email-analyst | 21 | 0 | 66s | $0.28 | 10.0h ago |
| email-classifier | 215 | 0 | 144s | $5.17 | 20m ago |
| email-responder | 49 | 0 | 90s | $6.24 | 10.3h ago |
| engine-report | 7 | 0 | 62s | $0.18 | 7.5h ago |
| evening-winddown | 7 | 1 | 75s | $2.59 | 9.5h ago |
| main | 254 | 0 | 101s | $21.47 | 5m ago |
| morning-briefing | 7 | 0 | 98s | $2.58 | 2s ago |
| vision-monitor | 21 | 0 | 95s | $0.30 | 18m ago |

## Tool Health (24h)
### Slowest Tools
- `look`: avg 48s (3 calls)
- `search_memory`: avg 39s (5 calls)
- `web_search`: avg 1s (9 calls)
### Most-Failing Tools
- `write_file`: 31/67 failed (46.3%)
- `exec`: 17/225 failed (7.6%)
- `search_memory`: 5/10 failed (50.0%)

