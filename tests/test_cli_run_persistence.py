"""`robothor run` must not lose its own run record.

Found live on 2026-08-24: a one-shot CLI run printed "Status: completed /
Model used: ollama_chat/qwen3.8:27b" — and its agent_runs row stayed stuck at
status=running with model_used NULL, until the zombie reaper later classified
the SUCCESSFUL run as a TIMEOUT. One-shot successes were being recorded as
failures, poisoning run-outcome stats and failure-streak detectors.

Root cause: `_finish_run` spawns DB persistence on the TaskRegistry when an
event loop is running — and inside `cmd_run` a loop IS running, so the spawn
path wins over the "CLI, tests (no loop)" sync fallback. `asyncio.run()` then
returns the moment `execute()` finishes and tears the loop down, cancelling
the pending `persist-run:<id>` task. The daemon drains its registry at
shutdown; the CLI never did.
"""

from __future__ import annotations

import argparse
import asyncio
from unittest.mock import MagicMock, patch


def _args(**overrides):
    defaults = {
        "agent": "probe",
        "message": "hello",
        "print_only": True,
        "json_output": False,
        "model": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _fake_run():
    from robothor.engine.models import RunStatus

    run = MagicMock()
    run.status = RunStatus.COMPLETED
    run.output_text = "ok"
    run.error_message = None
    run.duration_ms = 5
    run.input_tokens = 1
    run.output_tokens = 1
    run.total_cost_usd = 0.0
    run.model_used = "test-model"
    return run


def test_cmd_run_drains_background_persistence_before_the_loop_dies():
    """The registry's pending tasks — persist-run above all — must complete
    inside the loop cmd_run owns, not be cancelled by its teardown."""
    from robothor.cli.engine import cmd_run
    from robothor.engine.task_registry import get_task_registry

    persisted = []

    async def fake_execute(self, **kwargs):
        async def persist():
            # Yield first, exactly like the real persist coroutine would.
            await asyncio.sleep(0)
            persisted.append(kwargs.get("agent_id"))

        get_task_registry().spawn(persist(), name="persist-run:test")
        return _fake_run()

    agent_config = MagicMock()
    agent_config.name = "Probe"
    agent_config.model_primary = "test-model"

    with (
        patch("robothor.engine.runner.AgentRunner.execute", fake_execute),
        patch("robothor.engine.config.load_agent_config", return_value=agent_config),
        patch("robothor.engine.tools.set_runner"),
    ):
        rc = cmd_run(_args())

    assert rc == 0
    assert persisted == ["probe"], (
        "the persist task spawned during execute() was cancelled by loop "
        "teardown instead of being drained — the run record is lost"
    )


def test_engine_run_subcommand_drains_too():
    """`robothor engine run` is a second loop-owning one-shot path with the
    identical cancellation bug — and the first fix attempt broke it instead
    (a blind replace converted its return without adding the drain, so it
    returned None). Source-level pin: every `runner.execute(` call in the CLI
    module must be followed by a registry drain before its loop ends."""
    import re
    from pathlib import Path

    import robothor.cli.engine as cli_engine

    source = Path(cli_engine.__file__).read_text()
    calls = [m.start() for m in re.finditer(r"await runner\.execute\(", source)]
    assert len(calls) >= 2, "expected both one-shot paths"
    for pos in calls:
        window = source[pos : pos + 2500]
        assert "get_task_registry().drain" in window, (
            "a one-shot execute() path lacks the TaskRegistry drain — its "
            "persist-run task dies with the loop"
        )
