# robothor/engine/ — Agent Engine

The Python Agent Engine: LLM runner, tool registry, Telegram bot, scheduler, hooks, and workflow engine.

## Architecture

- **Fully async internals** — `asyncio.run()` only at `daemon.py` (systemd) and `cli.py` (CLI). Never add `asyncio.run()` inside library code.
- **FastAPI** on port 18800 (localhost only, tunneled via Cloudflare). Routes use `APIRouter` + `app.include_router()`.
- **Tools** registered in `tools.py`. Sync tools in `_handle_sync_tool()`, async in `_handle_async_tool()`.
- **Agent config** loaded from YAML manifests (`docs/agents/*.yaml`) by `config.py`. v2 features under `v2:` key.

## Key Entry Points

| File | Purpose |
|------|---------|
| `daemon.py` | Systemd entry point — starts scheduler, Telegram bot, health API |
| `cli.py` | CLI: `robothor engine {run,start,stop,status,list,history,workflow}` |
| `config.py` | YAML manifest → `AgentConfig` dataclass |
| `health.py` | FastAPI app creation, all `/health`, `/runs`, `/costs`, `/api/*` endpoints |
| `runner.py` | Core agent execution loop |
| `tools.py` | Tool registry and handlers |
| `scheduler.py` | Cron-based agent scheduling + heartbeat |

## Testing

```bash
pytest robothor/engine/tests/ -v --tb=short -m "not slow and not llm and not e2e"
```
