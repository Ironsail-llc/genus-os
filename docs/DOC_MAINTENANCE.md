# Doc Maintenance Checklist

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
| Federation changes | `docs/FEDERATION.md`, `INFRASTRUCTURE.md` (Federation section), `SERVICES.md` |
| New interactive mode | `brain/TOOLS.md`, `brain/AGENTS.md`, `CLAUDE.md` (reading guide), `SERVICES.md` (endpoints) |
| Vault credential changes | `brain/TOOLS.md` (Vault section), `INFRASTRUCTURE.md` (Secrets Management) |
| Deployment/fix with gotchas | Auto-memory `MEMORY.md` (session-to-session learning) |
| Task-lifecycle change (planner / promoter / todo escalation / autonomy defaults) | `docs/SYSTEM_ARCHITECTURE.md` (Task Lifecycle section), `docs/AGENT_BUILDER.md` (Section 2a), `brain/HEARTBEAT.md`, `brain/AGENTS.md`, `brain/SOUL.md`, `brain/memory/autonomy_defaults.md` if budgets shift |
