---
name: Morning Briefing
version: "2026-03-03"
description: Daily morning briefing with weather, news, calendar, and CRM activity
format: robothor-native/v1
department: briefings
---

# Morning Briefing

Delivers a daily morning briefing via Telegram. Covers weather, upcoming
calendar events, email summary, health data, and CRM activity.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/anthropic/claude-sonnet-4.6`)
- **cron_expr**: Cron schedule (default: `30 6 * * *` — 6:30 AM daily)
- **timezone**: Schedule timezone (default: `UTC`)
- **delivery_mode**: Delivery mode (default: `announce`)
- **delivery_channel**: Delivery channel (default: `telegram`)

## Dependencies

- Garmin health data sync (for health section)
- Email classifier status file (for inbox summary)
- Calendar data (for schedule)
