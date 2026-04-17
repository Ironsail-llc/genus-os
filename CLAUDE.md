# Genus OS — Project Root

Genus OS is a deterministic AI agent platform. Users deploy instances with their own identity, agents, and workflows.

## Platform vs Instance

This is the most important concept in the codebase. Getting it wrong leaks personal data into git.

| Layer | Location | Tracked | Purpose |
|-------|----------|---------|---------|
| **Platform** | `robothor/`, `crm/`, `infra/`, `app/`, `docs/*.md` | Yes | Core engine, tools, migrations, dashboard |
| **Instance** | `brain/`, `docs/agents/*.yaml`, `local/`, `.robothor/` | No | Identity, agent configs, memory, secrets |

**The rule**: Platform code must never contain instance-specific data — no personal names, email addresses, domains, phone numbers, home addresses, hardcoded tenant IDs, or absolute paths. Use environment variables and constants instead.

### What goes where

| I want to... | Put it in... | Why |
|--------------|-------------|-----|
| Add a new engine tool | `robothor/engine/tools/` | Platform — all instances get it |
| Configure my agent fleet | `docs/agents/*.yaml` + `brain/agents/*.md` | Instance — survives upgrades |
| Add a platform guardrail | Root `CLAUDE.md` (this file) | Platform — ships to everyone |
| Add a personal rule for Claude | `brain/CLAUDE.md` | Instance — only your machine |
| Write test fixtures | Use generic names (Alice, Bob, agent@example.com) | Platform — no personal data |
| Reference the operator | Say "the operator" not a personal name | Platform — works for any user |
| Set a tenant ID default | `os.environ.get("ROBOTHOR_DEFAULT_TENANT", "default")` | Platform — env var, not hardcoded |
| Reference workspace path | `os.environ.get("ROBOTHOR_WORKSPACE", ...)` | Platform — never `/home/username` |

### How Claude Code sees both layers

Claude Code reads every `CLAUDE.md` in the directory tree. So:
- `CLAUDE.md` (this file) — platform rules, always loaded
- `brain/CLAUDE.md` — instance rules, loaded if present, gitignored
- `robothor/engine/CLAUDE.md` — subsystem rules, tracked

Your `brain/CLAUDE.md` is where personal identity, preferences, and instance-specific rules go. It never enters git. To create one for a new instance, run `robothor init` or copy from `templates/`.

## Rules

1. **Never embed instance data in platform code** — no names, emails, domains, addresses, phone numbers, or hardcoded tenant IDs. Use `DEFAULT_TENANT` from `robothor/constants.py`, `ROBOTHOR_WORKSPACE` env var, and generic test fixtures. This is the #1 cause of data leaks.
2. **Canonical paths** — use `os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor")` — never hardcode absolute paths like `/home/username/robothor`.
3. **Never commit secrets** — SOPS-encrypted, decrypted to tmpfs at runtime. Use `os.getenv()` in Python, `$VAR` in shell. Gitleaks pre-commit hook enforces this.
4. **Engine is the execution layer, manifests are source of truth** — all agents run via `robothor/engine/`. YAML manifests in `docs/agents/` are canonical config.
5. **System-level systemd services** — every long-running process in `/etc/systemd/system/`, enabled on boot. `Restart=always`, `RestartSec=5`.
6. **Manifests are source of truth for models** — check `docs/agents/*.yaml` `model:` blocks for current assignments.
7. **No localhost URLs in agent instructions** — engine's `web_fetch` blocks loopback. Localhost is fine in internal code and infra docs.
8. **Test before commit** — pre-commit: `pytest -m "not slow and not llm and not e2e"`. Full: `bash run_tests.sh`. Tests alongside code: `<module>/tests/test_<feature>.py`. Mock LLMs in unit tests.
9. **Update docs with the change** — see `docs/DOC_MAINTENANCE.md` for the checklist.
10. **Async boundaries** — engine internals (`robothor/engine/`) are fully async. `asyncio.run()` only in entry points (daemon.py, cli.py) and standalone scripts.
11. **Instance data is user-land** — `brain/`, `docs/agents/*.yaml`, and `docs/CRON_MAP.md` are .gitignored. They belong to the instance, not the platform. Agent configs survive platform upgrades.
12. **Operator identity comes from `~/.robothor/owner.yaml`** — load it via `robothor.owner_config.load_owner_config()`, resolve the operator's CRM row via `robothor.crm.dal.get_owner_person(tenant_id)`. **Never search for the operator by first name** (e.g. `search_people("<operator-first-name>")`) — use `prefer_owner=True` if you must, or call `get_owner_person()` directly. The `.robothor/` dir is gitignored; the path is platform-hardcoded. New instances copy `templates/owner.yaml.example`.

## Operator Best Practices

These come from production experience running the first Genus OS instance:

- **Limit delivery agents** — only 2-3 agents should talk to the operator directly. Workers use `delivery: none` and communicate via CRM tasks and notifications.
- **Cloudflare tunnel for port-bearing services** — sensitive services use Cloudflare Access (email OTP). Public services have no auth.
- **Heartbeat, not polling** — use the main agent's heartbeat for decision-only updates, not per-agent delivery.

## Quick Reference

- **Platform vs instance boundary**: `docs/PLATFORM_INSTANCE.md`
- **Contributing guidelines**: `CONTRIBUTING.md`
- **System map, reading guide**: `docs/READING_GUIDE.md`
- **Doc update checklists**: `docs/DOC_MAINTENANCE.md`
- **System architecture**: `docs/SYSTEM_ARCHITECTURE.md`
- **Onboarding new instances**: `templates/CLAUDE.md`
