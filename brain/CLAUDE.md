# Robothor — Project Root

Robothor is an autonomous AI entity — Philip's partner, not an assistant. For identity and personality, read `brain/SOUL.md`.

## Identity

| Field | Value |
|-------|-------|
| Email | robothor@ironsail.ai |
| Phone | +1 (413) 408-6025 (Twilio — inbound + outbound voice) |
| Domain | robothor.ai |
| Telegram Bot | Robothor (main session delivery) |
| Home | 29 W 16th Road, Broad Channel, NY 11693 |

## System Map

| Path | Real Location | Purpose |
|------|--------------|---------|
| `brain/` | `~/robothor/brain/` | Core workspace: memory, scripts, voice, vision, dashboards, identity |
| `robothor/engine/` | In-repo Python package | Python Agent Engine: LLM runner, tool registry, Telegram bot, scheduler, hooks, workflow engine |
| `robothor/health/` | In-repo Python package | Garmin health data sync (every 15 min → PostgreSQL → daily memory) |
| `templates/` | (real directory) | Bootstrap templates for new Robothor instances |
| `tunnel/` | `~/.cloudflared/` | Cloudflare tunnel config (robothor.ai routes) |
| `crm/` | `robothor/crm/` | CRM stack: native PostgreSQL tables, Bridge, Docker Compose |

Symlinks are used for brain/, health/, templates/, tunnel/. All services and crons use absolute paths — nothing breaks.

## Rules

1. **Don't move directories** — all services and crons use absolute paths to `~/robothor/brain/`, `~/.openclaw/`, etc. Symlinks here are for navigation only.
2. **Never commit secrets** — all secrets live in SOPS-encrypted `/etc/robothor/secrets.enc.json`, decrypted to tmpfs at runtime. Use `os.getenv()` in Python, `$VAR` in shell. The gitleaks pre-commit hook blocks commits containing secrets. See `INFRASTRUCTURE.md` for SOPS workflow.
3. **Agent engine is the execution layer, manifests are source of truth** — all agents run via `robothor/engine/`. YAML manifests in `docs/agents/` are canonical config. Edit the manifest FIRST, then run `python scripts/validate_agents.py --agent <id>` and restart the engine.
4. **All services are system-level, use `sudo systemctl`** — every long-running process is a system-level systemd service in `/etc/systemd/system/`, enabled on boot. No user-level services. All use `Restart=always`, `RestartSec=5`, `KillMode=control-group`.
5. **Only 3 agents talk to Philip** — Main agent heartbeat (decisions-only), Morning Briefing (daily), Evening Wind-Down (daily). All worker agents use `delivery: none` and coordinate via tasks, status files, and notification inbox.
6. **Models vary per agent** — Email Responder uses Sonnet 4.6 (quality-critical). All other agents use Kimi K2.5. Main session uses Sonnet 4.6. Fallback: Kimi K2.5 → MiniMax M2.5 → Gemini 2.5 Pro. Local Ollama models (qwen3:14b) are only for Python maintenance scripts — not for agentic tool-calling.
7. **No localhost URLs in agent instructions** — the engine's `web_fetch` tool blocks loopback addresses. Use registered tools instead. Localhost is fine in internal code and infrastructure docs.
8. **All services with ports get Cloudflare tunnel routes** — internal/sensitive services use Cloudflare Access (email OTP for philip@ironsail.ai, robothor@ironsail.ai). Public services (status, voice, privacy) have no auth. SearXNG (:8888) is internal-only, no tunnel. See `SERVICES.md` and `INFRASTRUCTURE.md` for full route/port tables.
9. **Test before commit** — new features require tests, bug fixes require regression tests. Pre-commit: `pytest -m "not slow and not llm and not e2e"`. Full suite: `bash run_tests.sh`. Tests live alongside code: `<module>/tests/test_<feature>.py`. Test AI by properties (structure, types, ranges), not exact values. Mock LLMs in unit tests. Deep reference: `docs/TESTING.md`.
10. **Update docs with the change** — when adding services, agents, cron jobs, tools, or routes, update all affected docs in the same session. See Doc Maintenance section below for the checklist.

## Reading Guide

| Task | Read first |
|------|-----------|
| Working on vision | `brain/VISION.md` |
| Viewing the webcam | `https://cam.robothor.ai/webcam/` (Cloudflare Access) |
| Changing cron behavior | `brain/CRON_DESIGN.md` + `docs/agents/*.yaml` manifests + `docs/CRON_MAP.md` |
| Understanding memory/RAG | `brain/memory_system/MEMORY_SYSTEM.md` |
| Sending emails or calendar | `brain/TOOLS.md` (gog CLI section) |
| Voice calling | `brain/TOOLS.md` (voice section) + `brain/voice-server/` |
| Cloudflare tunnel routes | `brain/TOOLS.md` (Cloudflare section) |
| Adding new tunnel subdomain | `brain/TOOLS.md` (Cloudflare section — 4-step workflow) |
| Python Agent Engine | `robothor/engine/` package — runner, tools, session, config, Telegram, scheduler |
| Engine CLI | `robothor engine {run,start,stop,status,list,history,workflow}` |
| Using deep reasoning | `brain/TOOLS.md` (Deep Reasoning + /deep sections) |
| Engine API endpoints | `SERVICES.md` (Engine API Endpoints section) |
| Agent scaffold | `robothor agent scaffold <id> [--description "..."]` |
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
| Updating documentation | This file (Doc Maintenance section below) |

## Doc Maintenance

When infrastructure, agents, services, or cron jobs change, update docs as part of the same work — not as a follow-up.

| Change | Update these docs |
|--------|-------------------|
| New systemd service | `SERVICES.md`, `INFRASTRUCTURE.md` (tunnel table if port-bearing) |
| New cron job (system) | `docs/CRON_MAP.md`, `brain/CRON_DESIGN.md` (if architectural), `SERVICES.md` |
| New agent | `robothor agent scaffold <id>`, edit manifest + instruction file per contracts, `PLAYBOOK.md` fleet table, `brain/AGENTS.md`, `docs/CRON_MAP.md`, `validate_agents.py` |
| Modified agent config | Agent manifest YAML (update first), then `validate_agents.py --agent <id>` |
| New MCP/plugin tool | `brain/AGENTS.md` (tool list) |
| New Cloudflare route | `INFRASTRUCTURE.md` (tunnel table), `SERVICES.md` (external access table) |
| New database table | `INFRASTRUCTURE.md` |
| New interactive mode | `brain/TOOLS.md`, `brain/AGENTS.md`, `CLAUDE.md` (reading guide), `SERVICES.md` (endpoints) |
| Deployment/fix with gotchas | Auto-memory `MEMORY.md` (session-to-session learning) |
