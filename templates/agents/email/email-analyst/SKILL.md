---
name: Email Analyst
version: "2026-03-09"
description: Analyzes complex emails for structured response preparation
format: robothor-native/v1
department: email
---

# Email Analyst

Analyzes complex emails for structured response preparation. Receives tasks
from the email classifier, fetches threads, produces structured findings,
and creates follow-up tasks for the email responder.

## Variables

- **model_primary**: Primary LLM model (default: `ollama_chat/qwen3.5:122b`)
- **cron_expr**: Cron schedule (default: `30 8-20/6 * * *` — every 6h)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- Email classifier agent (upstream — creates tasks tagged `analytical`)
- Email responder agent (downstream — receives analysis tasks)
- `email` Redis stream (read)
- `response-analysis.json` shared state file
