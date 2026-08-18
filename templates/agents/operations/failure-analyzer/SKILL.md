---
name: Failure Analyzer
version: "2026-03-05"
description: Detects agent failures, classifies root causes, creates improvement tasks for overnight PR agent
format: robothor-native/v1
department: operations
---

# Failure Analyzer

Part of the Nightwatch self-healing system. Runs every 2 hours to detect
agent failures, classify root causes (transient, config, code, unknown),
and create actionable improvement tasks for the overnight PR agent.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/anthropic/claude-sonnet-4-6`)
- **cron_expr**: Cron schedule (default: `25 */2 * * *` — every 2h)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- `agent` and `system` Redis streams (read + write)
- Agent run analytics tools: `list_agent_runs`, `get_agent_run`, `get_agent_stats`
- Overnight PR agent (downstream — receives self-improve tasks)
- Memory blocks for pattern tracking
