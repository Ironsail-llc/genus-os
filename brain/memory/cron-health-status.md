# Cron Health Status
Updated: 2026-04-04 16:00 ET

## Fleet Summary (24h)
- Runs: 123 (117 ok, 1 failed, 4 timeout)
- Failure rate: 4%
- Cost: $3.23
- Avg duration: 67s

**1 ERROR**, 0 stale, 16 healthy (17 total)

## Errors (7d)
- **crm-steward**: 0 failed, 3 timeout (75% fail rate), last: 3.3d ago

## Healthy Agents (7d)
| Agent | Runs | Failed | Avg Duration | Cost | Last Run |
|-------|------|--------|-------------|------|----------|
| calendar-monitor | 257 | 0 | 74s | $4.90 | 3.9h ago |
| canary | 1 | 0 | 7s | $0 | 2.0d ago |
| chat-responder | 232 | 0 | 53s | $17.67 | 3.5h ago |
| conversation-inbox | 119 | 0 | 49s | $1.56 | 55m ago |
| conversation-resolver | 21 | 0 | 61s | $0.43 | 1.7h ago |
| crm-enrichment | 2 | 0 | 171s | $0.24 | 5.0h ago |
| crm-hygiene | 2 | 0 | 206s | $0.40 | 6.0h ago |
| email-analyst | 21 | 0 | 67s | $0.33 | 1.5h ago |
| email-classifier | 217 | 0 | 144s | $5.42 | 1.8h ago |
| email-responder | 49 | 0 | 121s | $7.39 | 1.8h ago |
| engine-report | 7 | 0 | 59s | $0.17 | 17.0h ago |
| evening-winddown | 7 | 1 | 74s | $2.09 | 19.0h ago |
| main | 314 | 1 | 97s | $22.76 | 2s ago |
| morning-briefing | 7 | 0 | 84s | $2.19 | 9.5h ago |
| proactive-check | 3 | 0 | 14s | $0.04 | 1.2d ago |
| vision-monitor | 21 | 0 | 90s | $0.27 | 3.8h ago |

## Tool Health (24h)
### Slowest Tools
- `store_memory`: avg 92s (11 calls)
- `look`: avg 15s (3 calls)
- `apollo_enrich_person`: avg 857ms (25 calls)
### Most-Failing Tools
- `apollo_search_people`: 18/18 failed (100.0%)
- `store_memory`: 10/21 failed (47.6%)
- `web_fetch`: 8/23 failed (34.8%)

