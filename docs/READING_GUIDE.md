# Reading Guide

`brain/`, tunnel configuration, systemd units, and host paths are
deployment-specific instance data and may be absent from a clean checkout. Use
the in-repository platform references below when an instance document is not
present; do not treat an instance path as a shipped security control.

## System Map

| Path | Real Location | Purpose |
|------|--------------|---------|
| `brain/` | `${ROBOTHOR_WORKSPACE}/brain/` (instance data) | Optional private workspace: memory, instructions, scripts, and identity |
| `robothor/engine/` | In-repo Python package | Python Agent Engine: LLM runner, tool registry, Telegram bot, scheduler, hooks, workflow engine |
| `app/` | In-repo Next.js application | The Helm dashboard/BFF; OIDC session boundary and read-only generated views |
| `robothor/health/` | In-repo Python package | Garmin health data sync (every 15 min → PostgreSQL → daily memory) |
| `templates/` | (real directory) | Bootstrap templates for new Genus OS instances |
| Cloudflare config | Operator-managed instance path | Optional tunnel/edge routing; not shipped authentication |
| `crm/` | In-repo directory | CRM stack: native PostgreSQL tables, Bridge, Docker Compose |
| `robothor/migrations/manifest.txt` | In-repo package data | Canonical ordered PostgreSQL migration chain |
| `helm/genus-os/` | In-repo Helm chart | Kubernetes workloads, secret classes, ServiceAccounts, readiness, and NetworkPolicy |

## What to Read First

| Task | Read first |
|------|-----------|
| Working on vision | `robothor/vision/` + `docs/SYSTEM_ARCHITECTURE.md` (reference-appliance section) |
| Viewing the webcam | `https://cam.${INSTANCE_DOMAIN}/webcam/` (Cloudflare Access) |
| Changing cron behavior | `docs/CRON_MAP.md` + the deployment's agent manifests/scheduler configuration |
| Understanding memory/RAG | `brain/memory_system/MEMORY_SYSTEM.md` |
| Sending emails or calendar | `brain/TOOLS.md` (gws native tools + gog CLI fallback) |
| Voice calling | `brain/TOOLS.md` (voice section) + `brain/voice-server/` |
| Cloudflare tunnel routes | `brain/TOOLS.md` (Cloudflare section) |
| Adding new tunnel subdomain | `brain/TOOLS.md` (Cloudflare section — 4-step workflow) |
| Python Agent Engine | `robothor/engine/` package — runner, tools, session, config, Telegram, scheduler |
| Engine CLI | `robothor engine {run,start,stop,status,list,history,workflow}` |
| Using deep reasoning | `brain/TOOLS.md` (Deep Reasoning + /deep sections) |
| Engine API endpoints | `SERVICES.md` (Engine API Endpoints section) |
| Engine HTTP/WebSocket authorization | `robothor/engine/auth.py` + `robothor/engine/tests/test_engine_auth.py` |
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
| Connecting an external API/MCP | `docs/CONNECTORS.md` (generic REST→MCP bridge for Claude Code + engine) |
| Hardware & infrastructure | `docs/SYSTEM_ARCHITECTURE.md` (reference appliance) + `docs/PLATFORM_INSTANCE.md` |
| Writing or running tests | `docs/TESTING.md` + `brain/memory_system/conftest.py` |
| Production go-live / hardening | `docs/PRODUCTION_HARDENING_TODO.md` |
| Security control scope and limitations | `docs/compliance/SECURITY_CONTROLS.md` |
| Dashboard OIDC/session boundary | `app/src/lib/auth.ts` + `app/src/proxy.ts` + `crm/bridge/routers/auth.py` |
| Read-only generated dashboards | `app/src/lib/dashboard/` + `app/src/components/canvas/srcdoc-renderer.tsx` + `robothor/engine/dashboards/completions.py` |
| Kubernetes deployment boundary | `helm/genus-os/README.md` + environment values |
| Database upgrades | `robothor/migrations/manifest.txt` + `robothor/db/migrate.py` + upgrade-safety integration test |
| Backup / restore | `docs/runbooks/SNAPSHOT_RESTORE.md` + `robothor snapshot --help` |
| Entity authority / treasury | `docs/ENTITY_KERNEL_TREASURY.md` |
| Customer or Genus payment data | `docs/compliance/PAYMENT_DATA.md` |
| Research notebooks (NotebookLM) | `nlm --help` (CLI) — auth: `nlm login`, check: `nlm login --check` |
| Managing agents | `docs/AGENT_BUILDER.md` |
| Building a new agent | `robothor agent scaffold <id>` + `docs/AGENT_BUILDER.md` (section 4) |
| Agent manifest schema | `docs/agents/schema.yaml` + `docs/AGENT_BUILDER.md` (section 4) |
| Instruction file contract | `docs/agents/INSTRUCTION_CONTRACT.md` |
| Rolling back an agent | Reviewed Git history for its manifest/instructions + `python scripts/validate_agents.py --agent <id>` |
| Agent validation | `python scripts/validate_agents.py` |
| Workflow engine | `docs/AGENT_BUILDER.md` (section 3) + `docs/workflows/*.yaml` + `robothor/engine/workflow.py` |
| Vault / credential storage | `robothor/vault/` + `brain/TOOLS.md` (Vault section) |
| Federation / multi-instance | `docs/FEDERATION.md` + `robothor/federation/` package |
| Federation CLI | `robothor federation {init,invite,connect,status,list,export,suspend,remove}` |
| NATS server (federation transport) | `docs/FEDERATION.md` + the deployment's NATS config/service definition |
| Updating documentation | `docs/DOC_MAINTENANCE.md` |
