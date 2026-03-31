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

## Rules

1. **Canonical paths** — services and crons use absolute paths to `~/robothor/brain/`. In Python, use `Path.home() / "robothor"` or `os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor")` — never hardcode `/home/philip/robothor`.
2. **Never commit secrets** — SOPS-encrypted `/etc/robothor/secrets.enc.json`, decrypted to tmpfs at runtime. Use `os.getenv()` in Python, `$VAR` in shell. Gitleaks pre-commit hook enforces this. See `INFRASTRUCTURE.md`.
3. **Engine is the execution layer, manifests are source of truth** — all agents run via `robothor/engine/`. YAML manifests in `docs/agents/` are canonical config. Edit manifest FIRST, then `python scripts/validate_agents.py --agent <id>` and restart.
4. **System-level systemd services** — every long-running process is in `/etc/systemd/system/`, enabled on boot. `Restart=always`, `RestartSec=5`. Use `sudo systemctl`.
5. **Only 3 agents talk to Philip** — Main heartbeat (decisions-only), Morning Briefing (daily), Evening Wind-Down (daily). Workers use `delivery: none`.
6. **Manifests are source of truth for models** — check `docs/agents/*.yaml` `model:` blocks for current assignments. For Ollama, use `ollama_chat/` prefix (not `ollama/`).
7. **No localhost URLs in agent instructions** — engine's `web_fetch` blocks loopback. Localhost is fine in internal code and infra docs.
8. **Cloudflare tunnel for all port-bearing services** — sensitive services use Cloudflare Access (email OTP). Public services have no auth. SearXNG (:8888) is internal-only. See `SERVICES.md`.
9. **Test before commit** — pre-commit: `pytest -m "not slow and not llm and not e2e"`. Full: `bash run_tests.sh`. Tests alongside code: `<module>/tests/test_<feature>.py`. Mock LLMs in unit tests. See `docs/TESTING.md`.
10. **Update docs with the change** — see `docs/DOC_MAINTENANCE.md` for the checklist.
11. **Async boundaries** — engine internals (`robothor/engine/`) are fully async. `asyncio.run()` only in entry points (daemon.py, cli.py) and standalone scripts.

## Quick Reference

- **System map, reading guide**: `docs/READING_GUIDE.md`
- **Doc update checklists**: `docs/DOC_MAINTENANCE.md`
- **System architecture**: `docs/SYSTEM_ARCHITECTURE.md`
