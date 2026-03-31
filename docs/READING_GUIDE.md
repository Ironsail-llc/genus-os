# Reading Guide

## System Map

| Path | Real Location | Purpose |
|------|--------------|---------|
| `brain/` | `~/robothor/brain/` | Core workspace: memory, scripts, voice, vision, dashboards, identity |
| `robothor/engine/` | In-repo Python package | Python Agent Engine: LLM runner, tool registry, Telegram bot, scheduler, hooks, workflow engine |
| `robothor/health/` | In-repo Python package | Garmin health data sync (every 15 min → PostgreSQL → daily memory) |
| `templates/` | (real directory) | Bootstrap templates for new Genus OS instances |
| `tunnel/` | `~/.cloudflared/` | Cloudflare tunnel config (robothor.ai routes) |
| `crm/` | `robothor/crm/` | CRM stack: native PostgreSQL tables, Bridge, Docker Compose |

## What to Read First

| Task | Read first |
|------|-----------|
| Working on vision | `brain/VISION.md` |
| Viewing the webcam | `https://cam.robothor.ai/webcam/` (Cloudflare Access) |
| Changing cron behavior | `brain/CRON_DESIGN.md` + `docs/agents/*.yaml` manifests + `docs/CRON_MAP.md` |
| Understanding memory/RAG | `brain/memory_system/MEMORY_SYSTEM.md` |
| Sending emails or calendar | `brain/TOOLS.md` (gws native tools + gog CLI fallback) |
| Voice calling | `brain/TOOLS.md` (voice section) + `brain/voice-server/` |
| Cloudflare tunnel routes | `brain/TOOLS.md` (Cloudflare section) |
| Adding new tunnel subdomain | `brain/TOOLS.md` (Cloudflare section — 4-step workflow) |
| Python Agent Engine | `robothor/engine/` package — runner, tools, session, config, Telegram, scheduler |
| Engine CLI | `robothor engine {run,start,stop,status,list,history,workflow}` |
| Using deep reasoning | `brain/TOOLS.md` (Deep Reasoning + /deep sections) |
| Engine API endpoints | `SERVICES.md` (Engine API Endpoints section) |
| Agent scaffold | `robothor agent scaffold <id> [--description "..."]` |
| Computer use / desktop control | `brain/agents/COMPUTER_USE.md` + `brain/TOOLS.md` (Desktop Control section) |
| Robothor's identity | `brain/SOUL.md` |
| Model selection | `brain/TOOLS.md` (Model Selection Guide) |
| Session startup (as Robothor) | `brain/AGENTS.md` |
| Health data | `robothor/health/` + `brain/memory/garmin-health.md` |
| CRM / contacts / conversations | `crm/` directory + `INFRASTRUCTURE.md` (CRM Stack section) |
| Bridge service / webhooks | `crm/bridge/bridge_service.py` |
| Contact resolution | `crm/bridge/contact_resolver.py` |
| Memory blocks | `brain/AGENTS.md` (Memory Blocks section) |
| Services & ports | `SERVICES.md` |
| Hardware & infrastructure | `INFRASTRUCTURE.md` |
| Writing or running tests | `docs/TESTING.md` + `brain/memory_system/conftest.py` |
| Backup / SSD / restore | `scripts/backup-ssd.sh` + `INFRASTRUCTURE.md` (External Storage) |
| Research notebooks (NotebookLM) | `nlm --help` (CLI) — auth: `nlm login`, check: `nlm login --check` |
| Managing agents | `docs/agents/PLAYBOOK.md` |
| Building a new agent | `robothor agent scaffold <id>` + `docs/agents/PLAYBOOK.md` (section 0) |
| Agent manifest schema | `docs/agents/schema.yaml` + `docs/agents/PLAYBOOK.md` (section 2) |
| Instruction file contract | `docs/agents/INSTRUCTION_CONTRACT.md` |
| Rolling back an agent | `docs/agents/PLAYBOOK.md` (section 4.3) |
| Agent validation | `python scripts/validate_agents.py` |
| Workflow engine | `docs/agents/PLAYBOOK.md` (section 8) + `docs/workflows/*.yaml` + `robothor/engine/workflow.py` |
| Vault / credential storage | `robothor/vault/` + `brain/TOOLS.md` (Vault section) |
| Federation / multi-instance | `docs/FEDERATION.md` + `robothor/federation/` package |
| Federation CLI | `robothor federation {init,invite,connect,status,list,export,suspend,remove}` |
| NATS server (federation transport) | `/etc/nats/nats-server.conf` + `robothor-nats.service` (ports 4222, 7422) |
| Updating documentation | `docs/DOC_MAINTENANCE.md` |
