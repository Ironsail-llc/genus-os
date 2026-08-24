"""Parallel fan-out/join for the workflow engine.

The engine was strictly sequential — five step types walked one at a time.
Every current pipeline pays for it in wall-clock (email-pipeline's classify
and calendar steps have no data dependency and still queue), and it was the
one orchestration feature separating Genus from the field: as of 2026-08,
OpenClaw schedules with cron+heartbeat only, Hermes has crons plus a delegate
tool, and DeepSeek Harness is a v0.1 preview — none has dependency-aware
fan-out/join. Genus already had the engine; this adds the step type.

Semantics under test:
  * `parallel` carries nested full step definitions; branches run
    concurrently, bounded by max_concurrent.
  * Each branch's result lands in run.context["steps"][branch_id] exactly like
    a top-level step, so later steps template on branch outputs.
  * A branch that fails after its own retry_count fails the parallel step;
    the parallel step's on_failure then decides (abort/skip) — same contract
    as every other step.
  * v1 keeps flow control at the top level: condition steps and nested
    parallel steps inside a parallel block are rejected AT PARSE, loudly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from robothor.engine.models import (
    RunStatus,
    WorkflowStepDef,
    WorkflowStepStatus,
    WorkflowStepType,
)
from robothor.engine.workflow import WorkflowEngine, parse_workflow


@pytest.fixture
def engine_config(tmp_path):
    from robothor.engine.config import EngineConfig

    return EngineConfig(workspace=tmp_path, manifest_dir=tmp_path / "agents")


def _make_engine(engine_config, steps, timeout_seconds: int = 60):
    from robothor.engine.models import WorkflowDef

    engine = WorkflowEngine(engine_config, runner=MagicMock())
    wf = WorkflowDef(id="test-wf", name="Test WF", steps=steps, timeout_seconds=timeout_seconds)
    engine._workflows["test-wf"] = wf
    engine._persist_run_start = MagicMock()
    engine._persist_run_end = MagicMock()
    engine._persist_step = MagicMock()
    return engine


def _transform_branch(step_id: str, expression: str = "value", **kw: Any) -> WorkflowStepDef:
    return WorkflowStepDef(
        id=step_id, type=WorkflowStepType.TRANSFORM, transform_expr=expression, **kw
    )


def _parallel_step(branches, step_id: str = "fan", **kw: Any) -> WorkflowStepDef:
    return WorkflowStepDef(
        id=step_id, type=WorkflowStepType.PARALLEL, parallel_steps=branches, **kw
    )


# ── Execution semantics ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_branches_run_and_results_land_in_context(engine_config):
    steps = [
        _parallel_step(
            [
                _transform_branch("fan.a", expression="alpha"),
                _transform_branch("fan.b", expression="beta"),
            ]
        ),
        WorkflowStepDef(
            id="join",
            type=WorkflowStepType.TRANSFORM,
            transform_expr="joined",
        ),
    ]
    engine = _make_engine(engine_config, steps)
    run = await engine.execute("test-wf")

    assert run.status == RunStatus.COMPLETED, run.error_message
    assert run.context["steps"]["fan.a"]["output_text"] == "alpha"
    assert run.context["steps"]["fan.b"]["output_text"] == "beta"
    assert run.context["steps"]["fan"]["status"] == "completed"
    assert run.context["steps"]["join"]["status"] == "completed"


@pytest.mark.asyncio
async def test_branches_actually_overlap(engine_config, monkeypatch):
    """Concurrency is the point — prove overlap, don't assume it."""
    starts: list[float] = []

    async def slow_agent_step(step, run, wf):
        from datetime import UTC, datetime

        from robothor.engine.models import WorkflowStepResult

        starts.append(time.monotonic())
        await asyncio.sleep(0.15)
        return WorkflowStepResult(
            step_id=step.id,
            step_type=step.type,
            status=WorkflowStepStatus.COMPLETED,
            started_at=datetime.now(UTC),
            output_text="done",
        )

    steps = [
        _parallel_step(
            [
                WorkflowStepDef(id="fan.a", type=WorkflowStepType.AGENT, agent_id="x"),
                WorkflowStepDef(id="fan.b", type=WorkflowStepType.AGENT, agent_id="x"),
                WorkflowStepDef(id="fan.c", type=WorkflowStepType.AGENT, agent_id="x"),
            ]
        )
    ]
    engine = _make_engine(engine_config, steps)
    monkeypatch.setattr(engine, "_execute_single_step", slow_agent_step)

    t0 = time.monotonic()
    run = await engine.execute("test-wf")
    elapsed = time.monotonic() - t0

    assert run.status == RunStatus.COMPLETED, run.error_message
    assert elapsed < 0.40, f"3 x 0.15s branches took {elapsed:.2f}s — they ran sequentially"
    assert max(starts) - min(starts) < 0.1, "branches did not start together"


@pytest.mark.asyncio
async def test_max_concurrent_bounds_the_fanout(engine_config, monkeypatch):
    running = 0
    peak = 0

    async def counting_step(step, run, wf):
        from datetime import UTC, datetime

        from robothor.engine.models import WorkflowStepResult

        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return WorkflowStepResult(
            step_id=step.id,
            step_type=step.type,
            status=WorkflowStepStatus.COMPLETED,
            started_at=datetime.now(UTC),
        )

    branches = [
        WorkflowStepDef(id=f"fan.{i}", type=WorkflowStepType.AGENT, agent_id="x") for i in range(4)
    ]
    steps = [_parallel_step(branches, max_concurrent=1)]
    engine = _make_engine(engine_config, steps)
    monkeypatch.setattr(engine, "_execute_single_step", counting_step)

    run = await engine.execute("test-wf")
    assert run.status == RunStatus.COMPLETED
    assert peak == 1, f"max_concurrent=1 but {peak} branches overlapped"


@pytest.mark.asyncio
async def test_failed_branch_aborts_by_default(engine_config):
    steps = [
        _parallel_step(
            [
                _transform_branch("fan.ok", expression="fine"),
                WorkflowStepDef(id="fan.bad", type=WorkflowStepType.TOOL, tool_name="no_such_tool"),
            ]
        ),
        _transform_branch("after", expression="reached"),
    ]
    engine = _make_engine(engine_config, steps)
    run = await engine.execute("test-wf")

    assert run.status == RunStatus.FAILED
    assert "fan" in (run.error_message or "")
    assert "after" not in run.context["steps"], "workflow continued past an aborting failure"


@pytest.mark.asyncio
async def test_failed_branch_with_skip_continues(engine_config):
    steps = [
        _parallel_step(
            [
                _transform_branch("fan.ok", expression="fine"),
                WorkflowStepDef(id="fan.bad", type=WorkflowStepType.TOOL, tool_name="no_such_tool"),
            ],
            on_failure="skip",
        ),
        _transform_branch("after", expression="reached"),
    ]
    engine = _make_engine(engine_config, steps)
    run = await engine.execute("test-wf")

    assert run.status == RunStatus.COMPLETED, run.error_message
    assert run.context["steps"]["after"]["output_text"] == "reached"
    # The good branch's output survives even though a sibling failed.
    assert run.context["steps"]["fan.ok"]["output_text"] == "fine"


@pytest.mark.asyncio
async def test_branch_retry_count_is_honored(engine_config, monkeypatch):
    attempts: dict[str, int] = {}

    async def flaky_step(step, run, wf):
        from datetime import UTC, datetime

        from robothor.engine.models import WorkflowStepResult

        attempts[step.id] = attempts.get(step.id, 0) + 1
        ok = attempts[step.id] >= 2
        return WorkflowStepResult(
            step_id=step.id,
            step_type=step.type,
            status=WorkflowStepStatus.COMPLETED if ok else WorkflowStepStatus.FAILED,
            started_at=datetime.now(UTC),
            error_message=None if ok else "flaky",
        )

    monkeypatch.setattr("robothor.engine.workflow._retry_delay", lambda attempt: 0)
    steps = [
        _parallel_step(
            [
                WorkflowStepDef(
                    id="fan.flaky", type=WorkflowStepType.AGENT, agent_id="x", retry_count=1
                )
            ]
        )
    ]
    engine = _make_engine(engine_config, steps)
    monkeypatch.setattr(engine, "_execute_single_step", flaky_step)

    run = await engine.execute("test-wf")
    assert run.status == RunStatus.COMPLETED, run.error_message
    assert attempts["fan.flaky"] == 2


# ── Parse-time guardrails ────────────────────────────────────────────────────


def _wf_dict(parallel_block: list[dict]) -> dict:
    return {
        "id": "wf",
        "steps": [{"id": "fan", "type": "parallel", "parallel_steps": parallel_block}],
    }


def test_parse_nested_parallel_is_rejected():
    with pytest.raises(ValueError, match="nested"):
        parse_workflow(_wf_dict([{"id": "fan.inner", "type": "parallel", "parallel_steps": []}]))


def test_parse_condition_inside_parallel_is_rejected():
    """Flow control stays at the top level in v1 — a condition's goto has no
    meaning inside a concurrent branch set."""
    with pytest.raises(ValueError, match="condition"):
        parse_workflow(_wf_dict([{"id": "fan.cond", "type": "condition", "input": "x"}]))


def test_parse_duplicate_branch_id_is_rejected():
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        parse_workflow(
            {
                "id": "wf",
                "steps": [
                    {
                        "id": "fan",
                        "type": "parallel",
                        "parallel_steps": [
                            {"id": "dup", "type": "transform", "expression": "'a'"},
                        ],
                    },
                    {"id": "dup", "type": "transform", "expression": "'b'"},
                ],
            }
        )


def test_parse_roundtrip_yaml_shape():
    wf = parse_workflow(
        _wf_dict(
            [
                {"id": "fan.a", "type": "transform", "expression": "'x'"},
                {"id": "fan.b", "type": "agent", "agent_id": "worker", "message": "go"},
            ]
        )
    )
    step = wf.steps[0]
    assert step.type == WorkflowStepType.PARALLEL
    assert [b.id for b in step.parallel_steps] == ["fan.a", "fan.b"]
    assert step.parallel_steps[1].agent_id == "worker"
