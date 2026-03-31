# brain/ — Robothor's Core Workspace

Memory, agent instructions, scripts, voice server, vision pipeline, and dashboards.

## Agent Instruction Files

Instruction files live in three locations:
- `brain/agents/` — most agents (CHAT_RESPONDER, FAILURE_ANALYZER, IMPROVEMENT_ANALYST, OVERNIGHT_PR, morning-briefing, evening-winddown)
- `brain/` — core agents (HEARTBEAT, RESPONDER, EMAIL_ANALYST, EMAIL_CLASSIFIER, CALENDAR_MONITOR, CONVERSATION_INBOX, CONVERSATION_RESOLVER, CRM_STEWARD, VISION_MONITOR, ENGINE_REPORT)
- `brain/instructions/` — CANARY

All instruction files follow the contract at `docs/agents/INSTRUCTION_CONTRACT.md`. Manifests in `docs/agents/*.yaml` are always edited first.

## Key Directories

- `memory/` — JSON state files read/written by agents (email-log, calendar-log, tasks, etc.)
- `memory_system/` — RAG pipeline, fact extraction, lifecycle. Docs: `memory_system/MEMORY_SYSTEM.md`
- `voice-server/` — Twilio + Gemini Live voice bridge. Docs: `TOOLS.md` (voice section)
- `scripts/` — Cron-triggered Python scripts. Design: `CRON_DESIGN.md`, map: `docs/CRON_MAP.md`

## Testing

```bash
pytest brain/memory_system/ -m "not slow"
```
