---
name: Email Classifier
version: "2026-03-03"
description: Classifies incoming emails, creates tasks, routes to analyst or responder
format: robothor-native/v1
department: email
---

# Email Classifier

Monitors the email inbox and classifies incoming messages. Creates tasks for
the email analyst (analytical queries) and email responder (replies needed).
Runs on a cron schedule with event hooks for real-time processing.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/moonshotai/kimi-k2.5`)
- **cron_expr**: Cron schedule (default: `0 6-22/2 * * *` — every 2h)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- Email sync cron (publishes to `email` Redis stream)
- `triage-inbox.json` file (created by email sync)
- Email analyst + responder agents (downstream)
