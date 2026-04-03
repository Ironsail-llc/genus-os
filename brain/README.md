# brain/

This directory contains your AI entity's identity, agent instructions, memory state, and runtime scripts.

It is **not tracked in git** — each installation gets its own `brain/` populated during setup.

## How it gets created

1. **`robothor init`** copies core files from `templates/` (SOUL.md, USER.md, IDENTITY.md, etc.)
2. **`robothor agent install`** copies agent instruction files and manifests
3. **Runtime agents** create and update memory files, status files, and logs

## Key contents

| Path | Purpose |
|------|---------|
| `SOUL.md` | AI personality and core truths |
| `IDENTITY.md` | Name, emoji, avatar |
| `USER.md` | Owner profile (filled in during onboarding) |
| `agents/` | Agent instruction files |
| `memory/` | Runtime state — logs, status files, task snapshots |
| `scripts/` | Cron-triggered Python scripts |
| `memory_system/` | RAG pipeline, fact extraction, lifecycle |

## See also

- `templates/` — source templates for brain files
- `docs/READING_GUIDE.md` — system map and architecture overview
