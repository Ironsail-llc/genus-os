"""Several tool calls in one turn, driven through the real loop.

`test_parallel_tools.py` pins the POLICY against a table of names. This file
pins the LOOP: that independent reads genuinely overlap, that a write does not,
that what comes back keeps the model's order and ids, that a refused call does
not hold up its siblings, and that a turn is one iteration however many calls
it carries.

The overlap assertions use a tool that records when it entered and left. That
is the only honest way to test concurrency here — a wall-clock comparison would
pass on a fast machine that ran everything sequentially.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode


def _tool_call(name: str, args: dict | None = None, call_id: str = "call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _response(content=None, tool_calls=None):
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


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    names = ["read_file", "web_fetch", "web_search", "write_file", "list_directory"]
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": n}} for n in names
        ]
        registry.get_tool_names.return_value = names
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="batch-agent",
        name="Batch Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
    )


class _Tracker:
    """Records the overlap of every tool call the registry was asked for."""

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.order: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def execute(self, name, args, **kwargs):
        self.order.append(name)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return {"ok": True, "tool": name}


async def _drive(runner, agent_config, calls, tracker=None, **kw):
    """One turn carrying `calls`, then a plain-text turn that ends the run."""
    turns = {"n": 0}
    status_events: list[dict] = []

    async def completion(**kwargs):
        turns["n"] += 1
        if turns["n"] == 1:
            return _response(tool_calls=calls)
        return _response(content="done")

    async def on_status(event):
        status_events.append(event)

    tracker = tracker or _Tracker()
    runner.registry.execute = AsyncMock(side_effect=tracker.execute)

    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        run = await runner.execute(
            "batch-agent", "go", agent_config=agent_config, on_status=on_status, **kw
        )
    return run, tracker, status_events


def _tool_messages(run, session_messages):
    return [m for m in session_messages if m.get("role") == "tool"]


@pytest.mark.usefixtures("_mock_run_persistence")
class TestIndependentReadsOverlap:
    @pytest.mark.asyncio
    async def test_three_reads_in_one_turn_run_at_the_same_time(self, runner, agent_config):
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("web_fetch", call_id="b"),
            _tool_call("web_search", call_id="c"),
        ]
        _, tracker, _ = await _drive(runner, agent_config, calls)
        assert tracker.peak >= 2, "three read-only calls in one turn never overlapped"

    @pytest.mark.asyncio
    async def test_the_limit_bounds_how_many_are_in_flight(self, runner, agent_config):
        calls = [_tool_call("read_file", call_id=f"r{i}") for i in range(6)]
        with patch("robothor.engine.tool_turn.parallel_limit", return_value=2):
            _, tracker, _ = await _drive(runner, agent_config, calls)
        assert tracker.peak <= 2

    @pytest.mark.asyncio
    async def test_a_limit_of_one_restores_sequential_execution(self, runner, agent_config):
        calls = [_tool_call("read_file", call_id=f"r{i}") for i in range(3)]
        with patch("robothor.engine.tool_turn.parallel_limit", return_value=1):
            _, tracker, _ = await _drive(runner, agent_config, calls)
        assert tracker.peak == 1


@pytest.mark.usefixtures("_mock_run_persistence")
class TestWritesSerialise:
    @pytest.mark.asyncio
    async def test_a_write_in_the_middle_stops_everything_after_it_overlapping(
        self, runner, agent_config
    ):
        calls = [
            _tool_call("write_file", call_id="w"),
            _tool_call("read_file", call_id="a"),
            _tool_call("web_fetch", call_id="b"),
        ]
        _, tracker, _ = await _drive(runner, agent_config, calls)
        assert tracker.peak == 1, "a write let the rest of the turn fan out beside it"

    @pytest.mark.asyncio
    async def test_the_write_never_moves_relative_to_its_neighbours(self, runner, agent_config):
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("write_file", call_id="w"),
            _tool_call("web_fetch", call_id="b"),
        ]
        _, tracker, _ = await _drive(runner, agent_config, calls)
        assert tracker.order == ["read_file", "write_file", "web_fetch"]


@pytest.mark.usefixtures("_mock_run_persistence")
class TestResultsKeepTheModelsOrder:
    @pytest.mark.asyncio
    async def test_step_rows_are_in_the_order_the_model_asked(self, runner, agent_config):
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("web_fetch", call_id="b"),
            _tool_call("web_search", call_id="c"),
        ]
        run, _, _ = await _drive(runner, agent_config, calls)
        tool_steps = [
            s for s in run.steps if s.tool_name in {"read_file", "web_fetch", "web_search"}
        ]
        assert [s.tool_name for s in tool_steps] == ["read_file", "web_fetch", "web_search"]

    @pytest.mark.asyncio
    async def test_every_call_gets_a_step_carrying_the_batch_and_its_position(
        self, runner, agent_config
    ):
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("web_fetch", call_id="b"),
        ]
        run, _, _ = await _drive(runner, agent_config, calls)
        batched = [s for s in run.steps if s.batch_id]
        assert len(batched) == 2
        assert len({s.batch_id for s in batched}) == 1, "one turn, one batch id"
        assert [s.batch_position for s in batched] == [0, 1]

    @pytest.mark.asyncio
    async def test_a_sequential_turn_carries_no_batch_id(self, runner, agent_config):
        calls = [_tool_call("write_file", call_id="w")]
        run, _, _ = await _drive(runner, agent_config, calls)
        assert all(s.batch_id is None for s in run.steps)


@pytest.mark.usefixtures("_mock_run_persistence")
class TestRefusalsAndAccounting:
    @pytest.mark.asyncio
    async def test_a_refused_call_does_not_block_its_siblings(self, runner, agent_config):
        """Plan mode refuses the write; the two reads beside it still run."""
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("write_file", call_id="w"),
            _tool_call("web_fetch", call_id="b"),
        ]
        run, tracker, _ = await _drive(runner, agent_config, calls, readonly_mode=True)
        assert tracker.order == ["read_file", "web_fetch"]
        refused = [s for s in run.steps if s.tool_name == "write_file"]
        assert refused and "plan mode" in (refused[0].error_message or "")

    @pytest.mark.asyncio
    async def test_the_refusal_keeps_its_place_in_the_trail(self, runner, agent_config):
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("write_file", call_id="w"),
            _tool_call("web_fetch", call_id="b"),
        ]
        run, _, _ = await _drive(runner, agent_config, calls, readonly_mode=True)
        wanted = {"read_file", "write_file", "web_fetch"}
        names = [s.tool_name for s in run.steps if s.tool_name in wanted]
        assert names == ["read_file", "write_file", "web_fetch"]

    @pytest.mark.asyncio
    async def test_a_turn_with_three_calls_is_one_iteration(self, runner, agent_config):
        """Not three. The whole point of batching is that the turn is the
        unit of accounting, so a fan-out must not cost three check-ins."""
        calls = [
            _tool_call("read_file", call_id="a"),
            _tool_call("web_fetch", call_id="b"),
            _tool_call("web_search", call_id="c"),
        ]
        _, _, events = await _drive(runner, agent_config, calls)
        starts = [e for e in events if e.get("event") == "iteration_start"]
        tools_started = [e for e in events if e.get("event") == "tools_start"]
        assert len(tools_started) == 1
        assert tools_started[0]["count"] == 3
        # Two iterations total: the tool turn, and the turn that answers.
        assert len(starts) == 2
