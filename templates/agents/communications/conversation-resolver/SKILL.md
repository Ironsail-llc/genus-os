---
name: Conversation Resolver
version: "2026-03-09"
description: Auto-resolves stale conversations older than 7 days
format: robothor-native/v1
department: communications
---

# Conversation Resolver

Keeps CRM conversations clean by auto-resolving stale threads. Scans open
conversations, checks last message timestamps, and resolves threads that
haven't had activity in 7+ days.

## Variables

- **model_primary**: Primary LLM model (default: `ollama_chat/qwen3.5:122b`)
- **cron_expr**: Cron schedule (default: `20 8,14,20 * * *` — 3x daily)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- `crm` Redis stream (read)
- CRM conversation and message tools
- Conversation inbox agent (handles urgent messages before resolution)
