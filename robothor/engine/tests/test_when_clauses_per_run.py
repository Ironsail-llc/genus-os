"""A manifest's ``when:`` clauses apply to the run that matches them.

``when:`` names a trigger type and the overrides a run of that type gets: an
interactive chat on the priority lane with a stronger model, a scheduled run on
the cheap chain. The merge has always supported it, but the run path never told
the loader which trigger it was loading for, so every run executed the
un-triggered document and the clauses did nothing.

These tests drive each entry point the engine runs an agent through and read
the configuration the run is actually handed: the model chain it would call and
the service tier it would ask for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from robothor.engine.config import EngineConfig, reset_defaults_cache
from robothor.engine.models import TriggerType

AGENT = "example-agent"
DEFAULT = ("openai/gpt-6-luna", ["vertex_ai/gemini-3.8-flash"], "default")
OVERRIDE = ("openai/gpt-6.2-sol", ["openai/gpt-6-luna"], "priority")

#: Every trigger a clause in the fixture names. HOOK and MANUAL are left out on
#: purpose: a run of either type must keep the fleet defaults.
CLAUSE_TRIGGERS = ["webchat", "cron", "telegram", "workflow", "event"]


def _chain(config: Any) -> tuple[str, list[str], str]:
    return (config.model_primary, list(config.model_fallbacks), config.service_tier)


def _write_fleet(manifest_dir, default=DEFAULT, override=OVERRIDE, agent=AGENT):
    """Fleet defaults, and one agent whose ``when:`` clauses carry ``override``."""
    keys = ("primary", "fallbacks", "service_tier")
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": agent,
        "name": "Example agent",
        "description": "A generic fixture agent",
        "version": "2026-11-17",
        "department": "operations",
        "schedule": {"cron": "0 7 * * *"},
        "when": [
            {"trigger_type": t, "overrides": {"model": dict(zip(keys, override, strict=True))}}
            for t in CLAUSE_TRIGGERS
        ],
    }
    defaults = {"model": dict(zip(keys, default, strict=True))}
    (manifest_dir / "_defaults.yaml").write_text(yaml.dump(defaults))
    (manifest_dir / f"{agent}.yaml").write_text(yaml.dump(manifest))
    reset_defaults_cache()
    return manifest_dir


@pytest.fixture(autouse=True)
def _no_last_resort(monkeypatch):
    import robothor.engine.config as config_mod

    # The instance's last-resort model is appended to every chain; keep it out
    # so the chain under test is exactly what the manifests say.
    monkeypatch.setattr(config_mod, "last_resort_model", lambda: "")
    yield
    reset_defaults_cache()


@pytest.fixture
def fleet(tmp_path):
    """A manifest dir with fleet defaults and one agent with ``when:`` clauses."""
    return _write_fleet(tmp_path / "docs" / "agents")


def _engine_config(manifest_dir) -> EngineConfig:
    return EngineConfig(
        manifest_dir=manifest_dir, workspace=manifest_dir.parent, tenant_id="test-tenant"
    )


def _capturing_runner(manifest_dir) -> tuple[MagicMock, list[Any]]:
    """A runner whose ``execute`` records the config it was handed."""
    seen: list[Any] = []

    async def execute(**kwargs: Any) -> Any:
        seen.append(kwargs.get("agent_config"))
        run = MagicMock()
        run.status.value = "completed"
        run.output_text = "done"
        return run

    runner = MagicMock()
    runner.config = _engine_config(manifest_dir)
    runner.execute = AsyncMock(side_effect=execute)
    return runner, seen


# ─── The runner's own lookup: every run that arrives without a config ────────


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (TriggerType.WEBCHAT, OVERRIDE),
        (TriggerType.CRON, OVERRIDE),
        (TriggerType.TELEGRAM, OVERRIDE),
        (TriggerType.WORKFLOW, OVERRIDE),
        (TriggerType.EVENT, OVERRIDE),
        (TriggerType.HOOK, DEFAULT),
        (TriggerType.MANUAL, DEFAULT),
    ],
)
async def test_the_runner_resolves_the_config_for_the_runs_trigger(fleet, trigger, expected):
    from robothor.engine.runner import AgentRunner

    runner = AgentRunner(_engine_config(fleet))
    config, refusal = await runner._config_or_refusal(
        AGENT, None, "hi", trigger, None, "test-tenant"
    )
    assert refusal is None
    assert _chain(config) == expected


# ─── Interactive admission (webchat, telegram) ──────────────────────────────


@pytest.mark.parametrize("trigger", ["webchat", "telegram"])
async def test_interactive_admission_resolves_the_config_for_the_runs_trigger(fleet, trigger):
    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest
    from robothor.engine.runtime.profile_admission import prepare

    runner = MagicMock()
    runner.config = _engine_config(fleet)
    request = RunRequest(
        context=ExecutionContext("test-tenant", "alice", "request-1"),
        agent_id=AGENT,
        message="hi",
        options={"trigger_type": trigger},
    )
    prepared, _resolution = await prepare(runner, request, datetime.now(UTC))
    assert _chain(prepared.options["agent_config"]) == OVERRIDE


# ─── Entry points that resolve the config before calling execute ────────────


async def test_a_cron_run_is_handed_the_cron_clause(fleet, monkeypatch):
    from robothor.engine.scheduler import CronScheduler

    scheduler = CronScheduler(_engine_config(fleet), MagicMock())
    handed: list[Any] = []

    async def run_scheduled(agent_id, dedup_key, agent_config, *args, **kwargs):
        handed.append(agent_config)

    monkeypatch.setattr(scheduler, "_run_scheduled", run_scheduled)
    await scheduler._run_agent(AGENT)
    assert _chain(handed[0]) == OVERRIDE


async def test_a_downstream_hook_run_keeps_the_fleet_defaults(fleet, monkeypatch):
    from robothor.engine import hooks
    from robothor.engine.hooks import EventHooks

    monkeypatch.setattr(hooks, "deliver", AsyncMock())
    runner, seen = _capturing_runner(fleet)
    await EventHooks(runner.config, runner)._trigger_downstream(AGENT, "email", "email.new")
    assert runner.execute.await_args.kwargs["trigger_type"] == TriggerType.HOOK
    assert _chain(seen[0]) == DEFAULT


async def test_a_manual_run_keeps_the_fleet_defaults(fleet):
    import asyncio

    from robothor.engine.manual_trigger import trigger_agent

    runner, seen = _capturing_runner(fleet)
    await trigger_agent(runner, fleet, AGENT, None)
    for _ in range(5):
        await asyncio.sleep(0)
    assert runner.execute.await_args.kwargs["trigger_type"] == TriggerType.MANUAL
    assert _chain(seen[0]) == DEFAULT


async def test_a_workflow_step_is_handed_the_workflow_clause(fleet, monkeypatch):
    from robothor.engine import delivery
    from robothor.engine.workflow import (
        WorkflowEngine,
        WorkflowStepDef,
        WorkflowStepResult,
    )

    monkeypatch.setattr(delivery, "deliver", AsyncMock())
    runner, seen = _capturing_runner(fleet)
    engine = WorkflowEngine(runner.config, runner)
    step = WorkflowStepDef(id="s1", agent_id=AGENT, message="go")
    run = MagicMock()
    run.context = {}
    await engine._run_agent_step(step, run, WorkflowStepResult(step_id="s1"))
    assert runner.execute.await_args.kwargs["trigger_type"] == TriggerType.WORKFLOW
    assert _chain(seen[0]) == OVERRIDE


def test_a_telegram_background_run_is_handed_the_telegram_clause(fleet):
    import dataclasses

    from robothor.engine.telegram import TelegramBot

    bot = object.__new__(TelegramBot)
    bot.config = dataclasses.replace(_engine_config(fleet), default_chat_agent=AGENT)
    assert _chain(bot._build_background_config()) == OVERRIDE


# ─── Loads that are not for a run keep the un-triggered document ────────────


def test_a_load_without_a_trigger_is_the_untriggered_document(fleet):
    from robothor.engine.config import load_agent_config

    assert _chain(load_agent_config(AGENT, fleet)) == DEFAULT


# ─── Reads made on behalf of a run, before it starts ────────────────────────

#: A model the registry says cannot be shown a picture, and one that can.
BLIND = ("openrouter/xiaomi/mimo-v2.5", [], "default")
SIGHTED = ("openai/gpt-6.2-sol", [], "default")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.mark.parametrize(
    ("default", "override", "shown"),
    [(BLIND, SIGHTED, True), (SIGHTED, BLIND, False)],
    ids=["clause-sees", "clause-blind"],
)
async def test_a_webchat_image_follows_the_webchat_clauses_model(
    tmp_path, default, override, shown
):
    """Whether an uploaded image reaches the model is decided before the run,
    and it has to be decided for the model the webchat run will call."""
    import base64
    from types import SimpleNamespace

    from robothor.engine.chat_attachments import prepare_turn

    fleet = _write_fleet(tmp_path / "docs" / "agents", default, override)
    body = {
        "attachments": [
            {"name": "shot.png", "mime": "image/png", "data": base64.b64encode(PNG).decode()}
        ]
    }
    turn = await prepare_turn(
        body,
        "look",
        session=SimpleNamespace(model_override=None),
        session_key=f"webchat:{AGENT}:alice",
        config=_engine_config(fleet),
    )
    assert bool(turn.images) is shown


def test_the_telegram_model_picker_shows_the_telegram_clauses_model(tmp_path):
    """`/model` names the model a telegram turn of main will run on."""
    from robothor.engine.telegram import TelegramBot

    fleet = _write_fleet(tmp_path / "docs" / "agents", agent="main")
    bot = object.__new__(TelegramBot)
    bot.config = _engine_config(fleet)
    assert bot._get_manifest_primary() == OVERRIDE[0]


def test_a_workflow_budget_check_sizes_the_step_by_the_workflow_clause(tmp_path):
    """A step runs under the workflow clause, so its budget is checked against
    that chain rather than the fleet's."""
    from robothor.engine import workflow_budget
    from robothor.engine.workflow import WorkflowEngine

    fleet = _write_fleet(tmp_path / "docs" / "agents")
    chains: list[list[str]] = []

    def report(wf, resolve_chain, **kwargs):
        chains.append(list(resolve_chain(AGENT)))
        raise RuntimeError("stop after resolving the chain")

    engine = WorkflowEngine(_engine_config(fleet), MagicMock())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workflow_budget, "report_step_budgets", report)
        engine._warn_budget_inversions(MagicMock(id="wf"))
    assert chains == [[OVERRIDE[0], *OVERRIDE[1]]]


async def test_a_goal_run_recorded_as_cron_is_handed_the_cron_clause(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from robothor.goals.controller import GoalController

    fleet = _write_fleet(tmp_path / "docs" / "agents", agent="main")
    handed: list[Any] = []

    async def run(request):
        handed.append(request)
        return SimpleNamespace(run=None)

    controller = GoalController(None, _engine_config(fleet), runtime=SimpleNamespace(run=run))
    goal = {"id": "goal-1", "parent_goal_id": None}
    with (
        patch.object(GoalController, "run_budget", staticmethod(lambda goal, config: 1000)),
        patch("robothor.goals.store.get", return_value={"objective": "x"}),
        patch("robothor.goals.store.finish"),
    ):
        await controller.execute(goal, "attempt-1")
    assert handed[0].options["trigger_type"] == TriggerType.CRON
    assert _chain(handed[0].options["agent_config"]) == OVERRIDE
