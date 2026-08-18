---
name: Email Responder
version: "2026-03-09"
description: Composes and sends email replies via gog gmail
format: robothor-native/v1
department: email
---

# Email Responder

Composes and sends email replies. Receives tasks from the email classifier,
fetches threads, looks up sender context, drafts replies, and sends them.
Supports review workflow for high-priority replies.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/z-ai/glm-5`)
- **cron_expr**: Cron schedule (default: `15 8-20/2 * * *` — every 2h)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- Email classifier agent (upstream — creates reply tasks)
- Email analyst agent (optional — provides structured analysis)
- `email` Redis stream (read)
- `gws` Gmail tools (send, search, get)
- `response-analysis.json` shared state file
