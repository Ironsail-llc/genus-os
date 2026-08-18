---
name: Vision Monitor
version: "2026-03-09"
description: Monitors vision events, escalates anomalies and unknown persons
format: robothor-native/v1
department: security
---

# Vision Monitor

Monitors camera feeds for person detection. Identifies known vs unknown
persons using face recognition and memory lookup. Escalates persistent
unknown persons or after-hours detections to the main agent.

## Variables

- **model_primary**: Primary LLM model (default: `ollama_chat/qwen3.5:122b`)
- **cron_expr**: Cron schedule (default: `12 6-22/6 * * *` — every 6h)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- `vision` Redis stream (read, hook trigger on `vision.person_unknown`)
- Vision tools: `look`, `who_is_here`, `set_vision_mode`
- Memory tools for person identification
- Main agent (receives escalation tasks)
