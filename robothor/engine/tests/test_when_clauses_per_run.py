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


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A manifest dir with fleet defaults and one agent with ``when:`` clauses."""
    import robothor.engine.config as config_mod

    # The instance's last-resort model is appended to every chain; keep it out
    # so the chain under test is exactly what the manifests say.
    monkeypatch.setattr(config_mod, "last_resort_model", lambda: "")
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    defaults = {"model": dict(zip(("primary", "fallbacks", "service_tier"), DEFAULT, strict=True))}
    override = {"model": dict(zip(("primary", "fallbacks", "service_tier"), OVERRIDE, strict=True))}
    manifest = {
        "id": AGENT,
        "name": "Example agent",
        "description": "A generic fixture agent",
        "version": "2026-11-17",
        "department": "operations",
        "schedule": {"cron": "0 7 * * *"},
        "when": [{"trigger_type": t, "overrides": override} for t in CLAUSE_TRIGGERS],
    }
    (manifest_dir / "_defaults.yaml").write_text(yaml.dump(defaults))
    (manifest_dir / f"{AGENT}.yaml").write_text(yaml.dump(manifest))
    reset_defaults_cache()
    yield manifest_dir
    reset_defaults_cache()


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
