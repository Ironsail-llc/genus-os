---
name: Conversation Inbox Monitor
version: "2026-03-09"
description: Checks for urgent unread messages in open conversations
format: robothor-native/v1
department: communications
---

# Conversation Inbox Monitor

Monitors open CRM conversations for urgent unread messages. Scans all open
conversations, applies urgency rules, and creates escalation tasks for the
main agent. Does not reply to messages — only triages and escalates.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/z-ai/glm-5`)
- **cron_expr**: Cron schedule (default: `5 6-22 * * *` — hourly)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- `crm` Redis stream (read)
- CRM conversation and message tools
- Main agent (receives escalation tasks)
