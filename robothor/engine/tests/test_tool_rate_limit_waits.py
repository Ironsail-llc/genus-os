"""A tool call over the per-minute limit waits for room; it is not refused.

An owner's chat turn had three tool calls refused with "Rate limit exceeded:
30/30 calls/min", and the agent then told the owner about "the rate_limit
policy gate". A burst is what reading ten things looks like, and an early
call is not a wrong call: the workflow engine has always waited in place for
the limiter (``workflow._run_tool_step``), and an agent run now does the same.

What bounds the wait is the run's own deadline, never a new timeout: when the
deadline would pass first, the call is not run and the run ends the way it
always ends at its deadline. What stops a run that calls tools forever is the
iteration safety cap, which the last class here pins.

No test here sleeps for real. The guardrail engine's clock and the
throttle's sleep are both replaced by a fake clock that the sleep advances,
the same convention ``test_deferred_runs`` and ``test_agentic_scenarios`` use.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus, StepType
from robothor.engine.session import AgentSession
from robothor.engine.tool_proxy import RunToolProxy, proxy_allow_set
from robothor.engine.tool_turn import ToolTurnRequest


class FakeClock:
    """A monotonic clock that only moves when the throttle sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    c = FakeClock()
    with (
        patch("robothor.engine.guardrails.time", SimpleNamespace(monotonic=c.monotonic)),
        patch("robothor.engine.tool_throttle.asyncio", SimpleNamespace(sleep=c.sleep)),
    ):
        yield c


def _engine(limit: int) -> GuardrailEngine:
    return GuardrailEngine(enabled_policies=["rate_limit"], rate_limit_per_minute=limit)


# ─── the measurement ─────────────────────────────────────────────────


class TestHowLongACallMustWait:
    def test_a_call_with_room_waits_for_nothing(self):
        assert _engine(3).rate_limit_wait("a") == 0.0

    def test_a_full_window_waits_until_its_oldest_call_ages_out(self):
        engine = _engine(2)
        with patch("robothor.engine.guardrails.time") as t:
            t.monotonic.return_value = 100.0
            engine.check_pre_execution("read_file", {}, agent_id="a")
            t.monotonic.return_value = 110.0
            engine.check_pre_execution("read_file", {}, agent_id="a")
            t.monotonic.return_value = 115.0
            assert engine.rate_limit_wait("a") == pytest.approx(45.0)

    def test_asking_counts_nothing(self):
        engine = _engine(2)
        for _ in range(5):
            engine.rate_limit_wait("a")
        assert engine.check_pre_execution("read_file", {}, agent_id="a").allowed
        assert engine.check_pre_execution("read_file", {}, agent_id="a").allowed

    def test_an_agent_without_the_policy_never_waits(self):
        engine = GuardrailEngine(enabled_policies=[], rate_limit_per_minute=1)
        for _ in range(5):
            engine.check_pre_execution("read_file", {}, agent_id="a")
        assert engine.rate_limit_wait("a") == 0.0

    def test_the_window_is_per_agent(self):
        engine = _engine(1)
        engine.check_pre_execution("read_file", {}, agent_id="noisy")
        assert engine.rate_limit_wait("noisy") > 0
        assert engine.rate_limit_wait("quiet") == 0.0


# ─── the inner calls of execute_code ─────────────────────────────────


@pytest.fixture
def proxy_runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.execute = AsyncMock(return_value={"ok": True})
        registry.registered_tool_names.return_value = {"read_file"}
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


def _proxy(runner, engine: GuardrailEngine) -> tuple[RunToolProxy, AgentSession]:
    config = AgentConfig(
        id="proxy-agent",
        name="Proxy Agent",
        model_primary="openrouter/test/model",
        delivery_mode=DeliveryMode.NONE,
    )
    session = AgentSession(agent_id=config.id)
    req = ToolTurnRequest(
        assistant_msg=MagicMock(tool_calls=[]),
        session=session,
        agent_config=config,
        iteration=0,
        guardrail_engine=engine,
        allowed_tool_set=frozenset({"read_file"}),
        readonly_tool_set=frozenset({"read_file"}),
    )
    proxy = RunToolProxy(
        runner=runner,
        req=req,
        allowed=proxy_allow_set(req, runner.registry),
        max_calls=20,
        max_approvals=1,
    )
    return proxy, session


@pytest.mark.asyncio
class TestASnippetsCallsWait:
    async def test_every_call_runs_and_the_overflow_waited(self, proxy_runner, clock):
        proxy, _ = _proxy(proxy_runner, _engine(2))
        results = [await proxy.call("read_file", {"path": f"{i}.txt"}) for i in range(3)]
        assert results == [{"ok": True}] * 3
        assert proxy_runner.registry.execute.await_count == 3
        assert clock.slept and sum(clock.slept) == pytest.approx(60.0)

    async def test_no_step_records_a_rate_limit_refusal(self, proxy_runner, clock):
        proxy, session = _proxy(proxy_runner, _engine(1))
        for i in range(4):
            await proxy.call("read_file", {"path": f"{i}.txt"})
        errors = [s.error_message for s in session.run.steps if s.error_message]
        assert errors == []


class _Watchdog:
    """The slice of the stall watchdog the throttle reads: the time left."""

    def __init__(self, remaining: float | None) -> None:
        self.remaining_seconds = remaining
        self.waits: list[tuple[str, float]] = []

    def begin_wait(self, label: str, budget: float) -> None:
        self.waits.append((label, budget))

    def end_wait(self) -> None:
        pass

    def touch(self, description: str = "") -> None:
        pass


@contextmanager
def _watching(watchdog: _Watchdog):
    """Bind the run's watchdog the way the runner does, for one block."""
    from robothor.engine.stall_watchdog import _active_watchdog_var

    token = _active_watchdog_var.set(watchdog)
    try:
        yield watchdog
    finally:
        _active_watchdog_var.reset(token)


@pytest.mark.asyncio
class TestTheRunsDeadlineBoundsTheWait:
    async def test_a_wait_past_the_deadline_does_not_run_the_call(self, proxy_runner, clock):
        engine = _engine(1)
        proxy, _ = _proxy(proxy_runner, engine)
        await proxy.call("read_file", {"path": "a"})
        with _watching(_Watchdog(remaining=5.0)):
            result = await proxy.call("read_file", {"path": "b"})

        assert proxy_runner.registry.execute.await_count == 1
        assert "error" in result
        assert sum(clock.slept) == pytest.approx(5.0)

    async def test_the_refusal_says_time_ran_out_not_rate_limit(self, proxy_runner, clock):
        engine = _engine(1)
        proxy, _ = _proxy(proxy_runner, engine)
        await proxy.call("read_file", {"path": "a"})
        with _watching(_Watchdog(remaining=5.0)):
            result = await proxy.call("read_file", {"path": "b"})

        text = json.dumps(result).lower()
        assert "rate" not in text
        assert "wall-clock budget" in text

    async def test_a_wait_inside_the_deadline_is_declared_to_the_watchdog(
        self, proxy_runner, clock
    ):
        """The stall clock must not read a throttle wait as a wedged run."""
        engine = _engine(1)
        proxy, _ = _proxy(proxy_runner, engine)
        await proxy.call("read_file", {"path": "a"})
        with _watching(_Watchdog(remaining=600.0)) as watchdog:
            result = await proxy.call("read_file", {"path": "b"})

        assert result == {"ok": True}
        assert watchdog.waits and watchdog.waits[0][1] == pytest.approx(60.0)


# ─── whole runs: single calls, a parallel batch, and a runaway ───────


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        registry.get_tool_names.return_value = ["list_tasks"]
        registry.execute = AsyncMock(return_value={"tasks": []})
        mock_reg.return_value = registry
        from robothor.engine.runner import AgentRunner

        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


def _config(**overrides) -> AgentConfig:
    config = AgentConfig(
        id="test-agent",
        name="Test Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=3600,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=["list_tasks"],
        planning_enabled=False,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _call(i: int) -> MagicMock:
    tc = MagicMock()
    tc.id = f"call_{i}"
    tc.function.name = "list_tasks"
    tc.function.arguments = json.dumps({"page": i})
    return tc


def _response(mock_litellm_response, *, calls: list[MagicMock] | None = None, text=None):
    response = mock_litellm_response(content=text, tool_calls=calls)
    if calls:
        response.choices[0].message.content = None
    return response


def _rate_limit_errors(run) -> list[str]:
    return [
        s.error_message
        for s in run.steps
        if s.error_message and "rate" in s.error_message.lower()
    ]


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.asyncio
class TestARunIsPacedNotRefused:
    async def test_single_calls_and_a_parallel_batch_all_run(
        self, runner, mock_litellm_response, clock
    ):
        """Three calls in one turn, then two in turns of their own, against a
        limit of two a minute: every one runs, and the model sees no refusal."""
        script = [
            _response(mock_litellm_response, calls=[_call(0), _call(1), _call(2)]),
            _response(mock_litellm_response, calls=[_call(3)]),
            _response(mock_litellm_response, calls=[_call(4)]),
            _response(mock_litellm_response, text="All five pages read."),
        ]
        sent: list[list[dict]] = []

        async def completion(**kwargs):
            sent.append(kwargs["messages"])
            return script[len(sent) - 1]

        with (
            patch("litellm.acompletion", side_effect=completion),
            patch.object(runner, "_apply_routing", return_value=None),
        ):
            run = await runner.execute(
                "test-agent", "read every page", agent_config=_config(rate_limit_per_minute=2)
            )

        assert run.status == RunStatus.COMPLETED
        assert runner.registry.execute.await_count == 5
        assert _rate_limit_errors(run) == []
        tool_results = [m for msgs in sent for m in msgs if m.get("role") == "tool"]
        assert all("rate" not in str(m.get("content", "")).lower() for m in tool_results)
        assert clock.slept, "the calls over the limit should have waited"

    async def test_the_prompt_states_the_agents_own_limit(
        self, runner, mock_litellm_response, clock
    ):
        sent: list[list[dict]] = []

        async def completion(**kwargs):
            sent.append(kwargs["messages"])
            return _response(mock_litellm_response, text="Done.")

        with (
            patch("litellm.acompletion", side_effect=completion),
            patch.object(runner, "_apply_routing", return_value=None),
        ):
            await runner.execute(
                "test-agent", "hello", agent_config=_config(rate_limit_per_minute=120)
            )

        system = sent[0][0]["content"]
        assert "120/minute" in system


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.asyncio
class TestARunawayStillStops:
    """With the throttle only pacing calls, the iteration safety cap is what
    ends a model that never stops asking for tools."""

    async def test_a_tool_spamming_model_ends_at_the_safety_cap(
        self, runner, mock_litellm_response, clock
    ):
        calls = 0

        async def completion(**kwargs):
            nonlocal calls
            calls += 1
            return _response(mock_litellm_response, calls=[_call(calls)])

        config = _config(rate_limit_per_minute=5, safety_cap=12)
        with (
            patch("litellm.acompletion", side_effect=completion),
            patch.object(runner, "_apply_routing", return_value=None),
        ):
            run = await runner.execute("test-agent", "loop forever", agent_config=config)

        stops = [s.tool_name for s in run.steps if s.step_type == StepType.BUDGET_STOP]
        assert "safety_cap" in stops
        assert runner.registry.execute.await_count == 12
        assert _rate_limit_errors(run) == []
        assert clock.slept, "a model past five calls a minute should have been paced"
