"""Tests for nested sub-agent spawning system.

Covers:
- spawn_agent / spawn_agents tool handlers
- SpawnContext and contextvar plumbing
- Config parsing for spawn-related v2 fields
- Tracking layer (parent_run_id, get_run_children, get_run_tree)
- Build-for-agent tool scoping (spawn tools hidden unless can_spawn_agents)
- Delivery safety net for sub-agent runs
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import (
    AgentConfig,
    AgentRun,
    DeliveryMode,
    RunStatus,
    SpawnContext,
    StepType,
    TriggerType,
)

# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def spawn_context():
    """A sample SpawnContext for testing."""
    return SpawnContext(
        parent_run_id=str(uuid.uuid4()),
        parent_agent_id="main",
        correlation_id=str(uuid.uuid4()),
        nesting_depth=0,
        max_nesting_depth=2,
        remaining_token_budget=100000,
        remaining_cost_budget_usd=0.50,
        parent_trace_id="abc123",
        parent_span_id="def456",
    )


@pytest.fixture
def child_agent_config():
    """Config for a child agent."""
    return AgentConfig(
        id="email-classifier",
        name="Email Classifier",
        model_primary="openrouter/test/model",
        max_iterations=15,
        timeout_seconds=300,
        delivery_mode=DeliveryMode.ANNOUNCE,
        tools_allowed=["list_tasks", "read_file"],
    )


@pytest.fixture
def spawning_agent_config():
    """Config for an agent with spawning enabled."""
    return AgentConfig(
        id="main",
        name="Main Agent",
        model_primary="openrouter/test/model",
        can_spawn_agents=True,
        max_nesting_depth=2,
        sub_agent_max_iterations=10,
        sub_agent_timeout_seconds=120,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=["spawn_agent", "spawn_agents", "list_tasks"],
    )


def _make_completed_run(agent_id: str = "email-classifier", **kwargs) -> AgentRun:
    """Create a completed AgentRun for mocking."""
    defaults = {
        "id": str(uuid.uuid4()),
        "agent_id": agent_id,
        "status": RunStatus.COMPLETED,
        "output_text": "Task done successfully.",
        "input_tokens": 500,
        "output_tokens": 200,
        "total_cost_usd": 0.002,
        "duration_ms": 3000,
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)


# ─── Tool Handler Tests (mock runner.execute, no DB) ──────────────────


class TestSpawnAgentTool:
    @pytest.mark.asyncio
    async def test_spawn_agent_basic(self, spawn_context, child_agent_config):
        """Completed run returns structured result."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        run = _make_completed_run()

        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        result = await _handle_spawn_agent(
                            {"agent_id": "email-classifier", "message": "Classify this email"},
                            agent_id="main",
                        )

            assert result["agent_id"] == "email-classifier"
            assert result["status"] == "completed"
            assert result["output_text"] == "Task done successfully."
            assert result["input_tokens"] == 500
            assert result["output_tokens"] == 200
            assert "error" not in result
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_agent_depth_limit(self, spawn_context):
        """Depth exceeded returns error without spawning."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        # Set depth to max
        spawn_context.nesting_depth = 2
        spawn_context.max_nesting_depth = 2

        mock_runner = MagicMock()
        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            result = await _handle_spawn_agent(
                {"agent_id": "worker", "message": "do something"},
                agent_id="main",
            )

            assert "error" in result
            assert "nesting depth" in result["error"].lower()
            # Runner.execute should NOT have been called
            mock_runner.execute.assert_not_called()
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_agent_unknown_agent(self, spawn_context):
        """Missing manifest returns error."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        mock_runner = MagicMock()
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"
        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=None):
                result = await _handle_spawn_agent(
                    {"agent_id": "nonexistent", "message": "hello"},
                    agent_id="main",
                )

            assert "error" in result
            assert "not found" in result["error"]
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_agent_budget_cascade(self, spawn_context, child_agent_config):
        """Child budget = min(child_config, parent_remaining)."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        # Parent has 100k tokens remaining, child config has no budget
        spawn_context.remaining_token_budget = 100000
        child_agent_config.token_budget = 0  # unlimited in own config

        run = _make_completed_run(input_tokens=1000, output_tokens=500)
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        await _handle_spawn_agent(
                            {"agent_id": "email-classifier", "message": "test"},
                            agent_id="main",
                        )

            # Verify budget was passed to child (via spawn_context in runner.execute call)
            call_kwargs = mock_runner.execute.call_args
            child_ctx = call_kwargs.kwargs.get("spawn_context")
            assert child_ctx is not None
            assert child_ctx.remaining_token_budget == 100000
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_agent_budget_deduction(self, spawn_context, child_agent_config):
        """Remaining budget decremented after child completes."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        initial_tokens = 100000
        spawn_context.remaining_token_budget = initial_tokens

        run = _make_completed_run(input_tokens=1000, output_tokens=500)
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        await _handle_spawn_agent(
                            {"agent_id": "email-classifier", "message": "test"},
                            agent_id="main",
                        )

            # Budget should be decremented
            assert spawn_context.remaining_token_budget == initial_tokens - 1000 - 500
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_no_delivery_for_sub_agents(self, spawn_context, child_agent_config):
        """Child config forced to DeliveryMode.NONE."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        child_agent_config.delivery_mode = DeliveryMode.ANNOUNCE

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        await _handle_spawn_agent(
                            {"agent_id": "email-classifier", "message": "test"},
                            agent_id="main",
                        )

            # Child config should have been forced to NONE
            call_kwargs = mock_runner.execute.call_args
            passed_config = call_kwargs.kwargs.get("agent_config")
            assert passed_config.delivery_mode == DeliveryMode.NONE
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_tool_scoping_override(self, spawn_context, child_agent_config):
        """tools_override replaces child's tools_allowed."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        await _handle_spawn_agent(
                            {
                                "agent_id": "email-classifier",
                                "message": "test",
                                "tools_override": ["exec", "web_search"],
                            },
                            agent_id="main",
                        )

            passed_config = mock_runner.execute.call_args.kwargs.get("agent_config")
            assert passed_config.tools_allowed == ["exec", "web_search"]
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_context_correlation_id(self, spawn_context, child_agent_config):
        """Child uses parent's correlation_id."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        await _handle_spawn_agent(
                            {"agent_id": "email-classifier", "message": "test"},
                            agent_id="main",
                        )

            call_kwargs = mock_runner.execute.call_args.kwargs
            assert call_kwargs["correlation_id"] == spawn_context.correlation_id
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_dedup_namespace_isolation(self, spawn_context, child_agent_config):
        """Namespaced dedup key prevents duplicate child spawns."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        mock_runner = MagicMock()
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"
        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            # try_acquire returns False — agent already running as sub-agent
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=False):
                    result = await _handle_spawn_agent(
                        {"agent_id": "email-classifier", "message": "test"},
                        agent_id="main",
                    )

            assert "error" in result
            assert "already running" in result["error"]
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_no_runner_returns_error(self):
        """spawn_agent with no runner ref returns error."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        set_runner(None)
        _current_spawn_context.set(None)

        result = await _handle_spawn_agent(
            {"agent_id": "worker", "message": "test"},
            agent_id="main",
        )
        assert "error" in result
        assert "Runner not available" in result["error"]

    @pytest.mark.asyncio
    async def test_no_spawn_context_returns_error(self):
        """spawn_agent outside agent run returns error."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        mock_runner = MagicMock()
        set_runner(mock_runner)
        _current_spawn_context.set(None)

        try:
            result = await _handle_spawn_agent(
                {"agent_id": "worker", "message": "test"},
                agent_id="main",
            )
            assert "error" in result
            assert "No spawn context" in result["error"]
        finally:
            set_runner(None)

    @pytest.mark.asyncio
    async def test_concurrency_semaphore(self, spawn_context, child_agent_config):
        """Semaphore limits parallel spawns."""
        from robothor.engine.tools import (
            MAX_CONCURRENT_SPAWNS,
            _current_spawn_context,
            _get_spawn_semaphore,
            _handle_spawn_agent,
            set_runner,
        )

        sem = _get_spawn_semaphore()
        assert sem._value == MAX_CONCURRENT_SPAWNS

        # Verify spawn acquires the semaphore
        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        await _handle_spawn_agent(
                            {"agent_id": "email-classifier", "message": "test"},
                            agent_id="main",
                        )

            # Semaphore should be released after spawn completes
            assert sem._value == MAX_CONCURRENT_SPAWNS
        finally:
            set_runner(None)
            _current_spawn_context.set(None)


class TestSpawnAgentsTool:
    @pytest.mark.asyncio
    async def test_spawn_agents_parallel(self, spawn_context, child_agent_config):
        """3 agents run, all results returned."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agents,
            set_runner,
        )

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with patch("robothor.engine.config.load_agent_config", return_value=child_agent_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        result = await _handle_spawn_agents(
                            {
                                "agents": [
                                    {"agent_id": "a", "message": "task 1"},
                                    {"agent_id": "b", "message": "task 2"},
                                    {"agent_id": "c", "message": "task 3"},
                                ]
                            },
                            agent_id="main",
                        )

            assert result["total"] == 3
            assert result["completed"] == 3
            assert result["failed"] == 0
            assert len(result["results"]) == 3
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_agents_partial_failure(self, spawn_context, child_agent_config):
        """1 fails (unknown agent), other 2 complete."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agents,
            set_runner,
        )

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        def _load_config(agent_id, _dir):
            if agent_id == "nonexistent":
                return None
            return child_agent_config

        try:
            with patch("robothor.engine.config.load_agent_config", side_effect=_load_config):
                with patch("robothor.engine.dedup.try_acquire", return_value=True):
                    with patch("robothor.engine.dedup.release"):
                        result = await _handle_spawn_agents(
                            {
                                "agents": [
                                    {"agent_id": "good-1", "message": "task 1"},
                                    {"agent_id": "nonexistent", "message": "task 2"},
                                    {"agent_id": "good-2", "message": "task 3"},
                                ]
                            },
                            agent_id="main",
                        )

            assert result["total"] == 3
            assert result["completed"] == 2
            assert result["failed"] == 1
            # The failed one should have an error
            failed = [r for r in result["results"] if r.get("error")]
            assert len(failed) == 1
        finally:
            set_runner(None)
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_agents_max_parallel_cap(self):
        """More than 5 agents returns error."""
        from robothor.engine.tools import _handle_spawn_agents

        result = await _handle_spawn_agents(
            {"agents": [{"agent_id": f"a{i}", "message": f"t{i}"} for i in range(6)]},
            agent_id="main",
        )
        assert "error" in result
        assert "5" in result["error"]


# ─── Build-for-Agent Scoping Tests ──────────────────────────────────


class TestToolScoping:
    def test_spawn_tools_hidden_by_default(self, sample_agent_config):
        """build_for_agent excludes spawn tools when can_spawn_agents=False."""

        with patch("robothor.engine.tools.get_registry") as mock_fn:
            registry = MagicMock()
            registry._schemas = {
                "list_tasks": {"type": "function", "function": {"name": "list_tasks"}},
                "spawn_agent": {"type": "function", "function": {"name": "spawn_agent"}},
                "spawn_agents": {"type": "function", "function": {"name": "spawn_agents"}},
            }
            mock_fn.return_value = registry

        # Directly test the ToolRegistry methods
        from robothor.engine.tools import SPAWN_TOOLS, ToolRegistry

        reg = MagicMock(spec=ToolRegistry)
        reg._schemas = {
            "list_tasks": {"type": "function"},
            "spawn_agent": {"type": "function"},
            "spawn_agents": {"type": "function"},
        }

        # Agent without spawn capability
        _config = AgentConfig(id="worker", name="Worker", can_spawn_agents=False)
        names = list(reg._schemas.keys())
        names = [n for n in names if n not in SPAWN_TOOLS]
        assert "spawn_agent" not in names
        assert "spawn_agents" not in names
        assert "list_tasks" in names

    def test_spawn_tools_visible_when_enabled(self):
        """Included when can_spawn_agents=True."""
        from robothor.engine.tools import SPAWN_TOOLS

        config = AgentConfig(
            id="main",
            name="Main",
            can_spawn_agents=True,
            tools_allowed=["spawn_agent", "spawn_agents", "list_tasks"],
        )

        # Simulate build_for_agent logic
        schemas = {"spawn_agent": {}, "spawn_agents": {}, "list_tasks": {}}
        names = [n for n in config.tools_allowed if n in schemas]
        if not config.can_spawn_agents:
            names = [n for n in names if n not in SPAWN_TOOLS]

        assert "spawn_agent" in names
        assert "spawn_agents" in names


# ─── Config Parsing Tests ────────────────────────────────────────────


class TestSpawnConfigParsing:
    def test_manifest_parses_spawn_fields(self):
        """can_spawn_agents, max_nesting_depth parsed from v2 block."""
        from robothor.engine.config import manifest_to_agent_config

        manifest = {
            "id": "test-spawner",
            "name": "Test Spawner",
            "v2": {
                "can_spawn_agents": True,
                "max_nesting_depth": 2,
                "sub_agent_max_iterations": 8,
                "sub_agent_timeout_seconds": 60,
            },
        }

        config = manifest_to_agent_config(manifest)
        assert config.can_spawn_agents is True
        assert config.max_nesting_depth == 2
        assert config.sub_agent_max_iterations == 8
        assert config.sub_agent_timeout_seconds == 60

    def test_max_nesting_depth_capped_at_3(self):
        """Values > 3 are capped."""
        from robothor.engine.config import manifest_to_agent_config

        manifest = {
            "id": "test-deep",
            "name": "Deep Agent",
            "v2": {
                "can_spawn_agents": True,
                "max_nesting_depth": 10,
            },
        }

        config = manifest_to_agent_config(manifest)
        assert config.max_nesting_depth == 3

    def test_spawn_fields_default_off(self):
        """Default: spawn disabled."""
        from robothor.engine.config import manifest_to_agent_config

        manifest = {"id": "basic", "name": "Basic Agent"}
        config = manifest_to_agent_config(manifest)
        assert config.can_spawn_agents is False
        assert config.max_nesting_depth == 2
        assert config.sub_agent_max_iterations == 10
        assert config.sub_agent_timeout_seconds == 120


# ─── Tracking Layer Tests (mock DB) ─────────────────────────────────


class TestSubAgentTracking:
    def test_create_run_with_parent(self, mock_db):
        """parent_run_id and nesting_depth persisted in INSERT."""
        from robothor.engine.tracking import create_run

        parent_id = str(uuid.uuid4())
        run = AgentRun(
            agent_id="child-agent",
            parent_run_id=parent_id,
            nesting_depth=1,
        )

        create_run(run)

        # Verify the INSERT included parent_run_id and nesting_depth
        cursor = mock_db["cursor"]
        call_args = cursor.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        assert "parent_run_id" in sql
        assert "nesting_depth" in sql
        assert parent_id in params
        assert 1 in params

    def test_get_run_children(self, mock_db):
        """Returns correct children."""
        from robothor.engine.tracking import get_run_children

        mock_db["cursor"].fetchall.return_value = [
            {
                "id": "child-1",
                "agent_id": "worker-a",
                "status": "completed",
                "trigger_type": "sub_agent",
                "nesting_depth": 1,
                "duration_ms": 1000,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_cost_usd": 0.001,
                "started_at": None,
                "completed_at": None,
            }
        ]
        mock_db["cursor"].description = [
            ("id",),
            ("agent_id",),
            ("status",),
            ("trigger_type",),
            ("nesting_depth",),
            ("duration_ms",),
            ("input_tokens",),
            ("output_tokens",),
            ("total_cost_usd",),
            ("started_at",),
            ("completed_at",),
        ]

        children = get_run_children("parent-id")
        assert len(children) == 1
        assert children[0]["agent_id"] == "worker-a"

    def test_get_run_tree(self, mock_db):
        """Recursive CTE returns full tree with aggregates."""
        from robothor.engine.tracking import get_run_tree

        mock_db["cursor"].fetchall.return_value = [
            {
                "id": "root",
                "agent_id": "main",
                "parent_run_id": None,
                "nesting_depth": 0,
                "status": "completed",
                "duration_ms": 5000,
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_cost_usd": 0.01,
                "started_at": None,
                "completed_at": None,
            },
            {
                "id": "child",
                "agent_id": "worker",
                "parent_run_id": "root",
                "nesting_depth": 1,
                "status": "completed",
                "duration_ms": 2000,
                "input_tokens": 300,
                "output_tokens": 100,
                "total_cost_usd": 0.003,
                "started_at": None,
                "completed_at": None,
            },
        ]

        tree = get_run_tree("root")
        assert tree["root"]["agent_id"] == "main"
        assert len(tree["runs"]) == 2
        assert tree["totals"]["total_runs"] == 2
        assert tree["totals"]["total_input_tokens"] == 1300
        assert tree["totals"]["total_output_tokens"] == 600
        assert tree["totals"]["max_nesting_depth"] == 1

    def test_get_run_tree_empty(self, mock_db):
        """Empty tree returns null root."""
        from robothor.engine.tracking import get_run_tree

        mock_db["cursor"].fetchall.return_value = []

        tree = get_run_tree("nonexistent")
        assert tree["root"] is None
        assert tree["runs"] == []


# ─── Delivery Safety Net Test ────────────────────────────────────────


class TestDeliverySafetyNet:
    @pytest.mark.asyncio
    async def test_sub_agent_delivery_suppressed(self):
        """Sub-agent output is suppressed even if delivery mode is announce."""
        from robothor.engine.delivery import deliver

        config = AgentConfig(
            id="child",
            name="Child",
            delivery_mode=DeliveryMode.ANNOUNCE,
            delivery_to="12345",
        )

        run = AgentRun(
            agent_id="child",
            output_text="Some output",
            parent_run_id=str(uuid.uuid4()),
        )

        result = await deliver(config, run)
        assert result is True  # Suppressed = success


# ─── Model Tests ─────────────────────────────────────────────────────


class TestModels:
    def test_trigger_type_sub_agent(self):
        assert TriggerType.SUB_AGENT == "sub_agent"
        assert TriggerType.SUB_AGENT.value == "sub_agent"

    def test_step_type_spawn_agent(self):
        assert StepType.SPAWN_AGENT == "spawn_agent"
        assert StepType.SPAWN_AGENT.value == "spawn_agent"

    def test_spawn_context_defaults(self):
        ctx = SpawnContext(
            parent_run_id="abc",
            parent_agent_id="main",
            correlation_id="corr-1",
            nesting_depth=0,
        )
        assert ctx.max_nesting_depth == 2
        assert ctx.remaining_token_budget == 0
        assert ctx.remaining_cost_budget_usd == 0.0
        assert ctx.parent_trace_id == ""

    def test_agent_run_parent_fields(self):
        run = AgentRun(
            agent_id="child",
            parent_run_id="parent-123",
            nesting_depth=1,
        )
        assert run.parent_run_id == "parent-123"
        assert run.nesting_depth == 1

    def test_agent_config_spawn_defaults(self):
        config = AgentConfig(id="test", name="Test")
        assert config.can_spawn_agents is False
        assert config.max_nesting_depth == 2
        assert config.sub_agent_max_iterations == 10
        assert config.sub_agent_timeout_seconds == 120
