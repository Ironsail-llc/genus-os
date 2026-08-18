---
name: Calendar Monitor
version: "2026-03-09"
description: Detects calendar conflicts, cancellations, and changes
format: robothor-native/v1
department: calendar
---

# Calendar Monitor

Monitors calendar events for conflicts, cancellations, and last-minute changes.
Processes triage inbox items, resolves scheduling-link tasks, and escalates
issues to the main agent.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/z-ai/glm-5`)
- **cron_expr**: Cron schedule (default: `8 6-22/6 * * *` — every 6h)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- `calendar` Redis stream (read, hook triggers)
- `gws` Calendar tools (list, create, delete)
- `triage-inbox.json` shared state file
- Main agent (receives escalation tasks)
