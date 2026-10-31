"""The Telegram-shaped half: N ``runner.execute`` turns in this process.

The same call the Telegram channel makes (``TriggerType.TELEGRAM``, no
``on_content``, so the model call does not stream), run against whatever the
environment configures: ``EngineConfig.from_env()``, the manifests in
``ROBOTHOR_MANIFEST_DIR`` and the database in ``ROBOTHOR_DB_*``. Run it inside
the engine container, or anywhere that environment is set.

Two things are observed from outside the runner, without changing it:

* the ``SETUP <n>ms`` log line, through a logging handler;
* every ``litellm.acompletion`` call, wrapped to time it and to estimate the
  first call's prompt size.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from bench.turn.dbstats import StatementCounter
from bench.turn.logparse import parse_setup_line
from bench.turn.steps import step_metrics
from bench.turn.tokens import estimate_prompt_tokens

if TYPE_CHECKING:
    from collections.abc import Callable


class _SetupCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.values: list[int] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            parsed = parse_setup_line(record.getMessage())
        except Exception:  # noqa: BLE001
            return
        if parsed:
            self.values.append(parsed.ms)


class _LlmCalls:
    """Wraps ``litellm.acompletion``; one list of (start, end, kwargs) per turn."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float, dict[str, Any]]] = []
        self._original: Any = None

    def install(self) -> None:
        import litellm

        original = litellm.acompletion
        self._original = original
        calls = self.calls

        async def timed(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            result = await original(*args, **kwargs)
            calls.append((start, time.perf_counter(), kwargs))
            return result

        litellm.acompletion = timed

    def uninstall(self) -> None:
        if self._original is not None:
            import litellm

            litellm.acompletion = self._original


def _step_dicts(run: Any) -> list[dict[str, Any]]:
    return [
        {
            "step_type": getattr(s.step_type, "value", s.step_type),
            "tool_name": s.tool_name,
            "duration_ms": s.duration_ms,
            "completed_at": s.completed_at,
        }
        for s in getattr(run, "steps", []) or []
    ]


async def _run(
    *,
    runs: int,
    agent: str,
    message: str,
    model: str,
    db_url: str,
    settle_ms: int,
    log: Callable[[str], None],
) -> dict[str, Any]:
    from robothor.engine.config import EngineConfig
    from robothor.engine.models import TriggerType
    from robothor.engine.runner import AgentRunner
    from robothor.engine.task_registry import get_task_registry
    from robothor.engine.tools import set_runner

    config = EngineConfig.from_env()
    runner = AgentRunner(config)
    set_runner(runner, config)
    agent_id = agent or config.default_chat_agent

    counter = StatementCounter(dsn=db_url)
    counter.open()
    notes: dict[str, str] = {}
    if not counter.available:
        notes["db_statements"] = counter.reason
        log(f"db_statements: off ({counter.reason})")

    setup = _SetupCapture()
    runner_logger = logging.getLogger("robothor.engine.runner")
    runner_logger.addHandler(setup)
    if runner_logger.getEffectiveLevel() > logging.INFO:
        runner_logger.setLevel(logging.INFO)
    llm = _LlmCalls()
    llm.install()

    turns: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    try:
        for i in range(runs):
            llm.calls.clear()
            setup.values.clear()
            before = counter.snapshot()
            t0 = time.perf_counter()
            run = await runner.execute(
                agent_id=agent_id,
                message=message,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"telegram:bench-{i}",
                model_override=model or None,
                tenant_id=config.tenant_id,
                user_id="telegram:bench",
                user_role="owner",
            )
            t_done = time.perf_counter()
            # What the daemon's loop would finish after the reply: the run's
            # background persist. Counted in db_statements, not in done_ms.
            await get_task_registry().drain(timeout=30.0)
            await asyncio.sleep(settle_ms / 1000.0)
            after = counter.snapshot()

            calls = list(llm.calls)
            steps = step_metrics(_step_dicts(run))
            first = calls[0] if calls else None
            turn: dict[str, Any] = {
                "setup_ms": setup.values[0] if setup.values else None,
                "setup_total_ms": steps["setup_total_ms"],
                "first_llm_ms": (first[1] - t0) * 1000.0 if first else None,
                "done_ms": (t_done - t0) * 1000.0,
                "finalize_tail_ms": (t_done - calls[-1][1]) * 1000.0 if calls else None,
                "tool_overhead_ms": steps["tool_overhead_ms"],
                "tool_ms": steps["tool_ms"] if steps["llm_calls"] else None,
                "db_statements": counter.delta(before, after),
                "prompt_tokens": (
                    estimate_prompt_tokens(first[2].get("messages") or [], first[2].get("tools"))
                    if first
                    else None
                ),
                "tool_schemas": len(first[2].get("tools") or []) if first else None,
            }
            turns.append(turn)
            status = getattr(getattr(run, "status", None), "value", "")
            meta.append(
                {
                    "turn": i,
                    "run_id": run.id,
                    "status": status,
                    "error": getattr(run, "error_message", None),
                }
            )
            log(
                f"inprocess turn {i + 1}/{runs}: done {turn['done_ms']:.0f} ms, "
                f"setup {turn['setup_ms']} ms, status {status}"
            )
    finally:
        llm.uninstall()
        runner_logger.removeHandler(setup)
        counter.close()
    return {"turns": turns, "meta": meta, "notes": notes}


def run_inprocess(
    *,
    runs: int,
    agent: str,
    message: str,
    model: str,
    db_url: str,
    settle_ms: int,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    return asyncio.run(
        _run(
            runs=runs,
            agent=agent,
            message=message,
            model=model,
            db_url=db_url,
            settle_ms=settle_ms,
            log=log,
        )
    )
