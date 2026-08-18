---
name: Evening Wind-Down
version: "2026-03-09"
description: Daily evening summary with tomorrow preview and open items
format: robothor-native/v1
department: briefings
---

# Evening Wind-Down

Delivers a reflective end-of-day summary to the owner via Telegram at 9 PM.
Covers tomorrow's calendar, completed tasks, open items, conversations,
email pipeline status, health stats, and a week-ahead glance.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/z-ai/glm-5`)
- **cron_expr**: Cron schedule (default: `0 21 * * *` — 9 PM daily)
- **timezone**: Schedule timezone (default: `UTC`)
- **delivery_mode**: Delivery mode (default: `announce`)

## Dependencies

- `email`, `calendar`, `health` Redis streams (read)
- `gws` Calendar tools
- Task and conversation listing tools
- Memory blocks: `user_profile`, `shared_working_state`
- Garmin health data file
