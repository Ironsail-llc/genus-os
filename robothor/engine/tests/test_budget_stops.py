"""Budgets that block become budgets that report.

A spend budget that stops an agent mid-workflow, silently, is the failure this
file is about: the operator sees a run that did not happen, or one that ended
early, and nothing that says which limit did it. Two properties are pinned:

* every budget, cap or stop that refuses, ends or degrades a run leaves ONE
  structured ``budget_stop`` step on that run -- the stop's name, the reason,
  the setting, the limit, the observed value and whether it was enforced -- and
  ONE ``agent.budget_stop`` event on the telemetry stream;
* a spend budget whose limit the operator never set observes rather than
  stops. Setting the limit explicitly, or setting its ``*_MODE`` to
  ``enforce``, keeps today's behaviour.

The durable operator stop is a control, not a budget: it still stops the run,
and it is recorded the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus, StepType
from robothor.engine.pool import init_fleet_pool
from robothor.engine.session import AgentSession

_FIELDS = {"stop", "reason", "setting", "limit", "observed", "mode"}


def _budget_steps(steps: list[Any]) -> list[Any]:
    return [s for s in steps if s.step_type == StepType.BUDGET_STOP]


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Every event the code under test put on the bus, captured at the source."""
    events: list[tuple[str, str, dict[str, Any]]] = []

    def _publish(stream: str, event_type: str, payload: dict[str, Any], **_kw: Any) -> str:
        events.append((stream, event_type, payload))
        return "1-0"

    monkeypatch.setattr("robothor.events.bus.publish", _publish)
    return events


@pytest.fixture
def _no_cap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ROBOTHOR_HOURLY_COST_CAP_USD",
        "ROBOTHOR_HOURLY_COST_CAP_MODE",
        "ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS",
        "ROBOTHOR_RUNAWAY_HARD_CAP_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def _admission_enforcing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admission at the rung production runs it at."""
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_GUARDRAILS", raising=False)
    monkeypatch.setenv("ROBOTHOR_ADMISSION_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_ADMISSION_MODE", "enforce")


@pytest.fixture
def _restore_pool():
    yield
    init_fleet_pool(max_concurrent=3, hourly_cost_cap_usd=5.0)


def _over_the_hourly_cap(mode: str) -> Any:
    """A real pool whose rolling hour has spent $6 against the $5 default cap."""
    from robothor.engine.config import EngineConfig

    cfg = EngineConfig.from_env()
    assert cfg.hourly_cost_cap_mode == mode, cfg.hourly_cost_cap_mode
    pool = init_fleet_pool(
        max_concurrent=3,
        hourly_cost_cap_usd=cfg.hourly_cost_cap_usd,
        cost_cap_mode=cfg.hourly_cost_cap_mode,
    )
    pool.register_run("earlier", "some-agent")
    pool.complete_run("earlier", cost_usd=6.0)
    return pool


def _cron_agent(**kw: Any) -> AgentConfig:
    return AgentConfig(
        id="crm-dedup",
        name="crm-dedup",
        model_primary="openrouter/test/model",
        timeout_seconds=0,
        department="crm",
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
        **kw,
    )


# ── The default: the hourly cap observes unless the operator set it ──────────


class TestTheHourlyCapDefault:
    def test_unset_cap_observes(self, _no_cap_env):
        from robothor.engine.config import EngineConfig

        cfg = EngineConfig.from_env()
        assert cfg.hourly_cost_cap_usd == 5.0
        assert cfg.hourly_cost_cap_mode == "observe"

    def test_an_explicit_cap_enforces(self, _no_cap_env, monkeypatch):
        from robothor.engine.config import EngineConfig

        monkeypatch.setenv("ROBOTHOR_HOURLY_COST_CAP_USD", "5")
        assert EngineConfig.from_env().hourly_cost_cap_mode == "enforce"

    def test_the_mode_enforces_without_an_explicit_cap(self, _no_cap_env, monkeypatch):
        from robothor.engine.config import EngineConfig

        monkeypatch.setenv("ROBOTHOR_HOURLY_COST_CAP_MODE", "enforce")
        assert EngineConfig.from_env().hourly_cost_cap_mode == "enforce"

    def test_the_mode_observes_even_with_an_explicit_cap(self, _no_cap_env, monkeypatch):
        """The operator's way out of a cap the deployment sets for them."""
        from robothor.engine.config import EngineConfig

        monkeypatch.setenv("ROBOTHOR_HOURLY_COST_CAP_USD", "5")
        monkeypatch.setenv("ROBOTHOR_HOURLY_COST_CAP_MODE", "observe")
        assert EngineConfig.from_env().hourly_cost_cap_mode == "observe"

    def test_an_observing_pool_admits_past_the_cap(self):
        from robothor.engine.pool import FleetPool

        pool = FleetPool(max_concurrent=3, hourly_cost_cap_usd=5.0, cost_cap_mode="observe")
        pool.register_run("r1", "a1")
        pool.complete_run("r1", cost_usd=6.0)
        assert pool.can_start("a2")[0] is True

    def test_concurrency_is_still_enforced_by_an_observing_pool(self):
        """Capacity is not spend: the slot cap is untouched."""
        from robothor.engine.pool import FleetPool, Priority

        pool = FleetPool(max_concurrent=1, hourly_cost_cap_usd=5.0, cost_cap_mode="observe")
        pool.register_run("r1", "a1")
        assert pool.can_start("a2", priority=Priority.BACKGROUND)[0] is False


# ── AC1: crossing the cap with the default config, the run completes ─────────


@contextlib.contextmanager
def _runner_without_infra(llm):
    from robothor.engine.stall_watchdog import _StallWatchdog

    with (
        patch.object(_StallWatchdog, "start", lambda self, task: None),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=llm),
        patch.object(AgentSession, "flush_new_steps_sync", lambda self: 0),
    ):
        yield


def _response(content: str | None = None, tool_calls: list[Any] | None = None) -> Any:
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


def _tool_call(i: int) -> Any:
    call = MagicMock()
    call.id = f"call_{i}"
    call.function.name = "exec"
    call.function.arguments = "{}"
    return call


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry"):
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "exec"}},
        ]
        registry.get_tool_names.return_value = ["exec"]
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


class TestCrossingTheCapByDefault:
    @pytest.mark.asyncio
    async def test_the_run_completes_and_carries_an_observe_step(
        self, _no_cap_env, _admission_enforcing, _restore_pool, runner, published, monkeypatch
    ):
        import robothor.engine.admission as adm

        monkeypatch.setattr(adm, "_record_deferral", lambda *a, **k: None)
        _over_the_hourly_cap("observe")

        assert adm.admit("crm-dedup", _cron_agent(), None) is True

        async def answer(**_kw):
            return _response(content="done")

        with _runner_without_infra(answer):
            run = await asyncio.wait_for(
                runner.execute("crm-dedup", "sweep", agent_config=_cron_agent()), timeout=60
            )

        assert run.status is RunStatus.COMPLETED
        steps = _budget_steps(run.steps)
        assert len(steps) == 1, [s.step_type for s in run.steps]
        detail = steps[0].tool_input
        assert set(detail) >= _FIELDS
        assert detail["stop"] == "hourly_cost_cap"
        assert detail["mode"] == "observe"
        assert detail["setting"] == "ROBOTHOR_HOURLY_COST_CAP_USD"
        assert detail["limit"] == 5.0
        assert detail["observed"] >= 6.0
        events = [e for e in published if e[1] == "agent.budget_stop"]
        assert len(events) == 1
        assert events[0][0] == "telemetry"
        assert events[0][2]["run_id"] == run.id
        assert events[0][2]["mode"] == "observe"

    @pytest.mark.asyncio
    async def test_an_explicit_cap_still_defers_and_the_step_says_enforce(
        self, _no_cap_env, _admission_enforcing, _restore_pool, published, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_HOURLY_COST_CAP_USD", "5")
        import robothor.engine.admission as adm

        created: list[Any] = []
        steps: list[Any] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.create_run", lambda run: created.append(run) or run.id
        )
        monkeypatch.setattr(
            "robothor.engine.tracking.create_step", lambda step: steps.append(step) or step.id
        )
        monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", lambda *a, **k: None)
        _over_the_hourly_cap("enforce")

        assert adm.admit("crm-dedup", _cron_agent(), None) is False

        assert len(created) == 1
        assert created[0].status is RunStatus.SKIPPED
        stops = _budget_steps(steps)
        assert len(stops) == 1
        assert stops[0].run_id == created[0].id
        assert stops[0].tool_input["mode"] == "enforce"
        assert stops[0].tool_input["stop"] == "hourly_cost_cap"
        assert [e[2]["mode"] for e in published if e[1] == "agent.budget_stop"] == ["enforce"]

    @pytest.mark.asyncio
    async def test_the_mode_setting_enforces_too(
        self, _no_cap_env, _admission_enforcing, _restore_pool, published, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_HOURLY_COST_CAP_MODE", "enforce")
        import robothor.engine.admission as adm

        monkeypatch.setattr("robothor.engine.tracking.create_run", lambda run: run.id)
        monkeypatch.setattr("robothor.engine.tracking.create_step", lambda step: step.id)
        monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", lambda *a, **k: None)
        _over_the_hourly_cap("enforce")
        assert adm.admit("crm-dedup", _cron_agent(), None) is False


# ── The runaway-token hard cap: observes unless set ─────────────────────────


def _session_over(tokens: int) -> AgentSession:
    session = AgentSession("runaway-agent")
    session.run.input_tokens = tokens
    session.run.output_tokens = 0
    return session


class TestTheRunawayTokenCap:
    def test_unset_cap_observes_once_and_still_alerts(self, _no_cap_env, published, monkeypatch):
        from robothor.engine import loop_guards

        monkeypatch.setattr("robothor.engine.runner.RUNAWAY_TOKEN_HARD_CAP", 1_000)
        monkeypatch.setattr("robothor.engine.runner.RUNAWAY_TOKEN_ALERT", 500)
        alerts: list[int] = []
        monkeypatch.setattr(
            loop_guards, "_spawn_hard_cap_alert", lambda _s, _c, used: alerts.append(used)
        )
        monkeypatch.setattr("robothor.engine.runner._send_soft_runaway_alert", lambda *a, **k: None)
        session = _session_over(2_000)
        cfg = _cron_agent()
        state = loop_guards.GuardState()

        assert loop_guards._runaway(session, cfg, state) is False
        assert loop_guards._runaway(session, cfg, state) is False

        steps = _budget_steps(session.run.steps)
        assert len(steps) == 1
        assert steps[0].tool_input["stop"] == "runaway_token_cap"
        assert steps[0].tool_input["mode"] == "observe"
        assert steps[0].tool_input["limit"] == 1_000
        assert steps[0].tool_input["observed"] == 2_000
        assert alerts == [2_000]
        assert session.run.budget_exhausted is not True

    def test_an_explicit_cap_stops_the_run_and_says_enforce(
        self, _no_cap_env, published, monkeypatch
    ):
        from robothor.engine import loop_guards

        monkeypatch.setenv("ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS", "1000")
        monkeypatch.setattr("robothor.engine.runner.RUNAWAY_TOKEN_HARD_CAP", 1_000)
        monkeypatch.setattr(loop_guards, "_spawn_hard_cap_alert", lambda *a: None)
        session = _session_over(2_000)

        assert loop_guards._runaway(session, _cron_agent(), loop_guards.GuardState()) is True
        steps = _budget_steps(session.run.steps)
        assert [s.tool_input["mode"] for s in steps] == ["enforce"]
        assert session.run.budget_exhausted is True


# ── Stops that keep stopping are recorded the same way ──────────────────────


class TestEnforcedStopsAreRecorded:
    def test_a_durable_stop_still_cancels_and_records_the_step(self, published):
        from robothor.engine.runtime.failure import failed_or_stopped
        from robothor.engine.runtime.provider_budget import DurableStopError

        session = AgentSession("stopped-agent")
        run = failed_or_stopped(
            session, DurableStopError("Durable stop denies another provider request"), None
        )

        assert run.status is RunStatus.CANCELLED
        steps = _budget_steps(session.run.steps)
        assert len(steps) == 1
        assert steps[0].tool_input["stop"] == "durable_stop"
        assert steps[0].tool_input["mode"] == "enforce"

    @pytest.mark.asyncio
    async def test_the_iteration_safety_cap_records_the_step(self, runner, published):
        calls = 0

        async def endless(**kw):
            nonlocal calls
            calls += 1
            if not kw.get("tools"):
                return _response(content="summary")
            return _response(tool_calls=[_tool_call(calls)])

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})
        with _runner_without_infra(endless):
            run = await asyncio.wait_for(
                runner.execute("crm-dedup", "loop", agent_config=_cron_agent(safety_cap=2)),
                timeout=60,
            )
        steps = _budget_steps(run.steps)
        assert len(steps) == 1, [s.step_type for s in run.steps]
        assert steps[0].tool_input["stop"] == "safety_cap"
        assert steps[0].tool_input["mode"] == "enforce"
        assert steps[0].tool_input["limit"] == 2


# ── Telemetry is published off the event loop ───────────────────────────────


class TestTelemetryNeverBlocksTheLoop:
    @pytest.mark.asyncio
    async def test_a_slow_bus_does_not_stall_the_loop(self, monkeypatch):
        from robothor.engine import budget_stops

        def _slow(*_a: Any, **_k: Any) -> None:
            time.sleep(0.5)

        monkeypatch.setattr("robothor.events.bus.publish", _slow)
        session = AgentSession("slow-bus")
        started = time.perf_counter()
        budget_stops.record(
            session,
            stop="safety_cap",
            reason="test",
            setting="safety_cap",
            limit=1,
            observed=1,
            mode="enforce",
        )
        elapsed = time.perf_counter() - started
        # Half the bus's sleep: an inline publish costs all of it, and a loaded
        # CI box must not be able to fail this on thread start-up alone.
        assert elapsed < 0.25, f"record() blocked the event loop for {elapsed:.3f}s"
        assert len(_budget_steps(session.run.steps)) == 1


# ── The step type is allowed by the database ────────────────────────────────


def test_every_run_step_type_is_allowed_by_the_check():
    """A step the CHECK rejects is a step nobody ever sees (see migration 062)."""
    from robothor.engine.tests.test_schema_drift import _newest_check_values

    allowed, source = _newest_check_values("agent_run_steps", "step_type")
    missing = {s.value for s in StepType} - set(allowed)
    assert not missing, f"{sorted(missing)} not allowed by {source.name}"


# ── An observed hourly cap raises the existing alert, once an hour ──────────


class TestTheObservedHourlyCapAlerts:
    @pytest.mark.asyncio
    async def test_crossing_the_cap_alerts_once_and_admits_every_run(
        self, _no_cap_env, _admission_enforcing, _restore_pool, published, monkeypatch
    ):
        import robothor.engine.admission as adm
        from robothor.engine import budget_stops

        monkeypatch.setattr(adm, "_record_deferral", lambda *a, **k: None)
        monkeypatch.setattr(budget_stops, "_last_alert", {})
        alerts: list[tuple[str, str]] = []

        async def _alert(level: str, title: str, body: str, **_kw: Any) -> bool:
            alerts.append((level, title))
            return True

        monkeypatch.setattr("robothor.engine.alerts.alert", _alert)
        _over_the_hourly_cap("observe")

        assert adm.admit("crm-dedup", _cron_agent(), None) is True
        assert adm.admit("crm-dedup", _cron_agent(), None) is True
        await asyncio.sleep(0)

        assert len(alerts) == 1, alerts
        assert alerts[0][0] == "warning"
        assert "hourly_cost_cap" in alerts[0][1]


# ── The modes are settings: config.yaml works as well as the environment ───


@pytest.fixture
def _config_yaml(tmp_path, monkeypatch, _no_cap_env):
    """Write the instance's config.yaml settings block; settings re-resolve."""
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_settings()

    def _write(body: str) -> None:
        (tmp_path / ".robothor").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".robothor" / "config.yaml").write_text(
            "settings:\n  providers:\n" + body, encoding="utf-8"
        )
        reset_settings()

    yield _write
    reset_settings()


class TestTheModesAreReadAsSettings:
    def test_hourly_mode_set_in_config_yaml_enforces(self, _config_yaml):
        """`genus config set ... enforce` writes config.yaml: it must take effect."""
        from robothor.engine.config import EngineConfig

        _config_yaml("    hourly_cost_cap_mode: enforce\n")
        assert EngineConfig.from_env().hourly_cost_cap_mode == "enforce"

    def test_an_hourly_cap_set_in_config_yaml_counts_as_explicit(self, _config_yaml):
        from robothor.engine.config import EngineConfig

        _config_yaml("    hourly_cost_cap_usd: 3.0\n")
        cfg = EngineConfig.from_env()
        assert cfg.hourly_cost_cap_usd == 3.0
        assert cfg.hourly_cost_cap_mode == "enforce"

    def test_runaway_mode_set_in_config_yaml_enforces(self, _config_yaml):
        from robothor.engine import budget_stops

        _config_yaml("    runaway_hard_cap_mode: enforce\n")
        assert budget_stops.runaway_hard_cap_mode() == "enforce"

    def test_nothing_set_anywhere_observes(self, _config_yaml):
        from robothor.engine import budget_stops
        from robothor.engine.config import EngineConfig

        _config_yaml("    last_resort_model: ''\n")
        assert EngineConfig.from_env().hourly_cost_cap_mode == "observe"
        assert budget_stops.runaway_hard_cap_mode() == "observe"


# ── The runaway pages say whether the run was stopped ───────────────────────


class TestTheRunawayAlertSaysWhatHappened:
    def test_the_hard_cap_page_says_the_run_continues_when_observing(
        self, _no_cap_env, monkeypatch
    ):
        from robothor.engine import loop_guards

        pages: list[tuple[str, str, str]] = []

        async def _alert(level: str, title: str, body: str, **_kw: Any) -> bool:
            pages.append((level, title, body))
            return True

        spawned: list[Any] = []
        registry = MagicMock()
        registry.spawn.side_effect = lambda coro, name=None: spawned.append(coro)
        monkeypatch.setattr("robothor.engine.alerts.alert", _alert)
        monkeypatch.setattr("robothor.engine.task_registry.get_task_registry", lambda: registry)
        loop_guards._spawn_hard_cap_alert(_session_over(6_000_000), _cron_agent(), 6_000_000)
        for coro in spawned:
            asyncio.run(coro)

        assert len(pages) == 1
        text = (pages[0][1] + " " + pages[0][2]).lower()
        assert "continu" in text
        assert "robothor_runaway_hard_cap_mode=enforce" in text

    @pytest.mark.parametrize("batched", [False, True])
    def test_the_soft_page_does_not_claim_containment_when_observing(
        self, _no_cap_env, monkeypatch, batched
    ):
        from robothor.engine import runner as runner_mod

        pages: list[str] = []

        async def _alert(level: str, title: str, body: str, **_kw: Any) -> bool:
            pages.append(body)
            return True

        spawned: list[Any] = []
        registry = MagicMock()
        registry.spawn.side_effect = lambda coro, name=None: spawned.append(coro)
        monkeypatch.setattr("robothor.engine.alerts.alert", _alert)
        monkeypatch.setattr("robothor.engine.task_registry.get_task_registry", lambda: registry)
        monkeypatch.setattr(runner_mod, "_soft_runaway_window_started_at", None)
        monkeypatch.setattr(
            runner_mod,
            "_soft_runaway_pending",
            [{"agent": "b", "run_id": "q", "tokens": 1}] if batched else [],
        )
        runner_mod._send_soft_runaway_alert("a", "r", 600_000, "m", 0.5)
        for coro in spawned:
            asyncio.run(coro)

        assert len(pages) == 1
        assert "contained by the budget guard" not in pages[0].lower()
        assert "robothor_runaway_hard_cap_mode=enforce" in pages[0].lower()


# ── An operator stop that lands mid-call is recorded when it is issued ──────


def _durable_steps(session: AgentSession) -> list[Any]:
    return [s for s in _budget_steps(session.run.steps) if s.tool_name == "durable_stop"]


class TestAStopDuringAModelCallIsRecorded:
    @pytest.mark.asyncio
    async def test_stop_local_records_the_step_and_cancels_the_call(self, published):
        """The in-flight call is cancelled, so no later provider request ever
        reaches the durable-stop check: the record has to be made here."""
        from robothor.engine.runtime import activity

        session = AgentSession("stopped-agent")
        session.run.tenant_id = "test-tenant"
        in_call = asyncio.Event()

        async def _model_call() -> None:
            in_call.set()
            await asyncio.sleep(30)

        task = asyncio.get_running_loop().create_task(_model_call())
        await in_call.wait()
        act = activity.Activity(task=task, loop=asyncio.get_running_loop())
        act.sessions[session.run_id] = session
        with activity._lock:
            activity._active[(session.run.tenant_id, session.run_id)] = act
        try:
            await asyncio.to_thread(
                activity.stop_local, session.run.tenant_id, session.run_id, "Operator stopped"
            )
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            activity.remove(act)

        steps = _durable_steps(session)
        assert len(steps) == 1
        assert steps[0].tool_input["mode"] == "enforce"
        assert steps[0].tool_input["stop"] == "durable_stop"
        assert "Operator stopped" in steps[0].tool_input["reason"]

    def test_signal_stopped_records_the_step_on_a_registered_session(self, published, monkeypatch):
        from robothor.engine import session_registry
        from robothor.engine.runtime import controls

        session = AgentSession("stopped-agent")
        session.run.tenant_id = "test-tenant"
        monkeypatch.setattr(controls, "stopped", lambda tenant, run_id: True)
        session_registry.register(session)
        try:
            controls.signal_stopped("test-tenant", "Operator stopped execution")
        finally:
            session_registry.unregister(session.run_id)

        assert len(_durable_steps(session)) == 1
        assert session._interrupt_requested is True

    def test_a_later_provider_refusal_does_not_record_it_twice(self, published, monkeypatch):
        from robothor.engine import session_registry
        from robothor.engine.runtime import controls
        from robothor.engine.runtime.failure import failed_or_stopped
        from robothor.engine.runtime.provider_budget import DurableStopError

        session = AgentSession("stopped-agent")
        session.run.tenant_id = "test-tenant"
        monkeypatch.setattr(controls, "stopped", lambda tenant, run_id: True)
        session_registry.register(session)
        try:
            controls.signal_stopped("test-tenant", "Operator stopped execution")
        finally:
            session_registry.unregister(session.run_id)
        failed_or_stopped(
            session, DurableStopError("Durable stop denies another provider request"), None
        )

        assert len(_durable_steps(session)) == 1
