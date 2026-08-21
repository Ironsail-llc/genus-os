# robothor/engine/ — Agent Engine

The Python Agent Engine: LLM runner, tool registry, Telegram bot, scheduler, hooks, and workflow engine.

## Architecture

- **Fully async internals** — `asyncio.run()` only at `daemon.py` (systemd) and `cli.py` (CLI). Never add `asyncio.run()` inside library code.
- **FastAPI** on port 18800 (localhost only, tunneled via Cloudflare). Routes use `APIRouter` + `app.include_router()`.
- **Tools** live in the `tools/` package: schemas in `tools/schemas.py`, per-agent filtering in `tools/registry.py`, dispatch + permission gate in `tools/dispatch.py`, and one handler module per domain under `tools/handlers/`. (The old monolithic `tools.py` / `_handle_sync_tool` / `_handle_async_tool` split is gone.)
- **LLM dispatch** (model fallback, streaming, cost, prompt-cache kwargs, message hygiene) lives in `llm_client.LLMClient`, not in `runner.py`. `AgentRunner` owns one `self._llm` and the tool loop delegates to it. Extend `LLMClient` for provider/dispatch changes; keep `runner.py` focused on orchestration.
- **Agent config** loaded from YAML manifests (`docs/agents/*.yaml`) by `config.py`. v2 features under `v2:` key.
- **Delivery accounting is verified, never assumed.** Scheduled/workflow/hook runs go through `delivery.deliver()`; interactive Telegram turns don't, so `telegram.TelegramBot._record_interactive_delivery()` writes `agent_runs.delivery_status` for them. Both derive the status from the sender's return value (`send_message` returns one `Message` per delivered chunk, `[]` when every chunk failed) — the same `delivered = bool(sent)` rule `alerts.py` uses. Never mark a send delivered just because the next line ran.

## Key Entry Points

| File | Purpose |
|------|---------|
| `daemon.py` | Systemd entry point — starts scheduler, Telegram bot, health API |
| `cli.py` | CLI: `robothor engine {run,start,stop,status,list,history,workflow}` |
| `config.py` | YAML manifest → `AgentConfig` dataclass |
| `health.py` | FastAPI app creation, all `/health`, `/runs`, `/costs`, `/api/*` endpoints |
| `runner.py` | `AgentRunner` — orchestration + the tool loop (`_run_loop`) |
| `llm_client.py` | `LLMClient` — LLM dispatch, model fallback, streaming, cost |
| `tools/` | Tool schemas (`schemas.py`), registry/filtering (`registry.py`), dispatch (`dispatch.py`), handlers (`handlers/`) |
| `scheduler.py` | Cron-based agent scheduling + heartbeat |

## Testing

```bash
pytest robothor/engine/tests/ -v --tb=short -m "not slow and not llm and not e2e"
```
