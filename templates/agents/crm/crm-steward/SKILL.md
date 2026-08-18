---
name: CRM Steward
version: "2026-03-09"
description: CRM data hygiene, duplicate merge, and contact enrichment
format: robothor-native/v1
department: crm
---

# CRM Steward

Maintains CRM data quality through multi-phase operations: task hygiene,
blocklist scanning, field scrubbing, duplicate detection and merging,
company hygiene, and contact enrichment via sub-agent research.

## Variables

- **model_primary**: Primary LLM model (default: `openrouter/z-ai/glm-5`)
- **cron_expr**: Cron schedule (default: `0 10 * * *` — daily at 10 AM)
- **timezone**: Schedule timezone (default: `UTC`)
- **reports_to**: Supervisor agent (default: `main`)

## Dependencies

- `crm` Redis stream (read + write)
- CRM people, company, and merge tools
- Notification inbox for review workflow
- Main agent (receives review requests and escalations)
