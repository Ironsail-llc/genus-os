# Genus OS — Project Root

Genus OS is a deterministic AI agent platform. Users deploy instances with their own identity, agents, and workflows. For instance identity, read `brain/SOUL.md` (user-land, not tracked in git).

## Rules

1. **Canonical paths** — use `os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor")` — never hardcode absolute paths.
2. **Never commit secrets** — SOPS-encrypted `/etc/robothor/secrets.enc.json`, decrypted to tmpfs at runtime. Use `os.getenv()` in Python, `$VAR` in shell. Gitleaks pre-commit hook enforces this.
3. **Engine is the execution layer, manifests are source of truth** — all agents run via `robothor/engine/`. YAML manifests in `docs/agents/` are canonical config.
4. **System-level systemd services** — every long-running process in `/etc/systemd/system/`, enabled on boot. `Restart=always`, `RestartSec=5`. Use `sudo systemctl`.
5. **Manifests are source of truth for models** — check `docs/agents/*.yaml` `model:` blocks for current assignments.
6. **No localhost URLs in agent instructions** — engine's `web_fetch` blocks loopback. Localhost is fine in internal code and infra docs.
7. **Cloudflare tunnel for port-bearing services** — sensitive services use Cloudflare Access. See `SERVICES.md`.
8. **Test before commit** — pre-commit: `pytest -m "not slow and not llm and not e2e"`. Full: `bash run_tests.sh`. Tests alongside code: `<module>/tests/test_<feature>.py`. Mock LLMs in unit tests. See `docs/TESTING.md`.
9. **Update docs with the change** — see `docs/DOC_MAINTENANCE.md` for the checklist.
10. **Async boundaries** — engine internals (`robothor/engine/`) are fully async. `asyncio.run()` only in entry points (daemon.py, cli.py) and standalone scripts.
11. **Instance data is user-land** — `brain/`, `docs/agents/*.yaml`, and `docs/CRON_MAP.md` are .gitignored. They belong to the instance, not the platform. Agent configs survive platform upgrades.

## Quick Reference

- **System map, reading guide**: `docs/READING_GUIDE.md`
- **Doc update checklists**: `docs/DOC_MAINTENANCE.md`
- **System architecture**: `docs/SYSTEM_ARCHITECTURE.md`
