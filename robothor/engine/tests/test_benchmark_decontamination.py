"""Benchmark-harness traffic must never be counted as production work.

Measured on this instance over 30 days of ``agent_runs``:

    all runs 4267 | benchmark 2685 (63%) | spend $78.03 total, $29.93 benchmark
    agent-architect  170 benchmark vs  19 production runs; 44/44 timeouts benchmark
    email-analyst    143 benchmark vs   0 production runs — graded on nothing
    crm_tasks        6,887 rows titled "<Agent>: sub_agent run" filed by benchmarks

Three symptoms, one root cause: ``_benchmark_run`` spawns each task through
``runner.execute`` WITHOUT a ``SpawnContext``
(``robothor/engine/tools/handlers/benchmark.py``), so every benchmark sub-run
records ``parent_run_id = NULL`` — the exact shape ``analytics.py`` uses in
twelve places to mean "top-level production run" — and the runner's auto-task
guard (``if agent_config.auto_task and not spawn_context``) files an
operator-facing CRM task for each one.

These tests pin all three, plus a drift guard so the analytics filter can
never fork into twelve hand-maintained copies again.
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import (
    AgentConfig,
    DeliveryMode,
    RunStatus,
    TriggerType,
)
from robothor.engine.tools.dispatch import ToolContext

PARENT_RUN_ID = "11111111-2222-3333-4444-555555555555"
CTX = ToolContext(
    agent_id="benchmark-runner",
    run_id=PARENT_RUN_ID,
    workspace="/tmp/test-workspace",
)


# ─── Helpers (mirrors test_benchmark.py) ─────────────────────────────


def _mock_blocks():
    """In-memory memory-block store with read/write functions."""
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-08-21T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _block_patches(read_fn, write_fn):
    return (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
    )


def _make_mock_run(status: str = "completed"):
    run = MagicMock()
    run.output_text = "ok"
    run.total_cost_usd = 0.01
    run.steps = [MagicMock()]
    run.status = MagicMock(value=status)
    run.id = "child-run-1"
    run.input_tokens = 10
    run.output_tokens = 5
    run.error_message = None
    return run


def _suite(agent_id: str = "email-analyst") -> str:
    return json.dumps(
        {
            "id": "s1",
            "agent_id": agent_id,
            "max_cost_usd": 1.0,
            "tasks": [
                {
                    "id": "t1",
                    "prompt": "x",
                    "category": "correctness",
                    "weight": 1.0,
                    "expected": {"must_contain": ["ok"]},
                }
            ],
        }
    )


@pytest.fixture(autouse=True)
def _isolate_benchmark_results_db(monkeypatch):
    """Keep _benchmark_run's benchmark_results write-through off any real DB."""

    class _FakeCursor:
        def execute(self, *a, **kw):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import robothor.crm.dal as _dal

    monkeypatch.setattr(_dal, "get_connection", lambda: _FakeConn())


@pytest.fixture
def enforce_decontamination(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE", "enforce")


@pytest.fixture
def observe_decontamination(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE", "observe")


async def _run_suite(mock_runner, agent_cfg) -> None:
    from robothor.engine.tools.handlers.benchmark import _benchmark_run

    store, read_fn, write_fn = _mock_blocks()
    store["benchmark:email-analyst:s1"] = _suite()
    p1, p2 = _block_patches(read_fn, write_fn)
    with (
        p1,
        p2,
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=mock_runner),
        patch("robothor.engine.config.load_agent_config", return_value=agent_cfg),
    ):
        await _benchmark_run(
            {"agent_id": "email-analyst", "suite_id": "s1", "tag": "t"},
            CTX,
        )


def _benchmark_child_config() -> MagicMock:
    cfg = MagicMock()
    cfg.max_iterations = 10
    cfg.cost_budget_usd = 1.0
    cfg.tools_allowed = ["read_file"]
    cfg.tools_denied = []
    cfg.is_benchmark = False
    cfg.model_primary = "openrouter/test/model"
    return cfg


# ─── (a) benchmark sub-runs must record a parent ─────────────────────


class TestBenchmarkSubRunLineage:
    @pytest.mark.asyncio
    async def test_benchmark_run_passes_a_spawn_context(self, enforce_decontamination):
        """Every benchmark task must be spawned with parent linkage."""
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=_make_mock_run())
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp"

        await _run_suite(mock_runner, _benchmark_child_config())

        kwargs = mock_runner.execute.await_args.kwargs
        spawn_context = kwargs.get("spawn_context")
        assert spawn_context is not None, (
            "benchmark_run spawned a sub-agent with no SpawnContext — the child "
            "records parent_run_id NULL and every `parent_run_id IS NULL` "
            "analytics filter counts it as production work"
        )
        assert spawn_context.parent_run_id == PARENT_RUN_ID
        assert kwargs["trigger_type"] is TriggerType.SUB_AGENT

    @pytest.mark.asyncio
    async def test_spawned_run_row_has_non_null_parent_run_id(
        self, enforce_decontamination, engine_config
    ):
        """End to end: the AgentRun recorded for a benchmark task has a parent.

        Takes the SpawnContext the benchmark handler actually builds and feeds
        it to a real AgentRunner.execute, asserting on the row handed to
        ``create_run`` — the thing analytics later reads.
        """
        from robothor.engine.runner import AgentRunner

        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=_make_mock_run())
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp"
        await _run_suite(mock_runner, _benchmark_child_config())
        spawn_context = mock_runner.execute.await_args.kwargs.get("spawn_context")
        assert spawn_context is not None

        child_config = AgentConfig(
            id="email-analyst",
            name="Email Analyst",
            model_primary="openrouter/test/model",
            timeout_seconds=30,
            delivery_mode=DeliveryMode.NONE,
            can_spawn_agents=False,
            planning_enabled=False,
            scratchpad_enabled=False,
            is_benchmark=True,
        )

        recorded: list = []
        with (
            patch("robothor.engine.runner.get_registry") as mock_reg,
            patch("robothor.engine.runner.create_run", side_effect=recorded.append),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch"),
            patch("robothor.engine.tracking.create_steps_batch"),
            patch("robothor.engine.tracking.create_step"),
            patch("litellm.acompletion", side_effect=[_llm_response("done")]),
        ):
            registry = MagicMock()
            registry.build_for_agent.return_value = []
            registry.get_tool_names.return_value = []
            mock_reg.return_value = registry
            runner = AgentRunner(engine_config)
            runner.registry = registry
            run = await runner.execute(
                "email-analyst",
                "benchmark task",
                trigger_type=TriggerType.SUB_AGENT,
                trigger_detail="benchmark:s1:t1",
                agent_config=child_config,
                spawn_context=spawn_context,
            )

        assert run.status is RunStatus.COMPLETED
        assert recorded, "run was never recorded"
        assert recorded[0].parent_run_id == PARENT_RUN_ID, (
            "benchmark sub-run recorded parent_run_id=NULL — indistinguishable "
            "from a top-level production run"
        )

    @pytest.mark.asyncio
    async def test_off_mode_keeps_the_legacy_shape(self, monkeypatch):
        """Flag off (the merge default until promoted) changes nothing."""
        monkeypatch.delenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED", raising=False)
        monkeypatch.delenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE", raising=False)

        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=_make_mock_run())
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp"

        await _run_suite(mock_runner, _benchmark_child_config())

        assert mock_runner.execute.await_args.kwargs.get("spawn_context") is None


def _llm_response(content: str):
    response = MagicMock()
    response.model = "openrouter/test/model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    response.usage = usage
    return response


# ─── (b) analytics must exclude benchmark traffic ────────────────────


@pytest.mark.integration
class TestProductionRunFilterExcludesBenchmarks:
    """The historical shape: parent_run_id NULL *and* a benchmark trigger.

    2,685 rows on this box already look like that; fixing the spawn context
    only helps future runs, so the filter must key on trigger_detail too.
    """

    @staticmethod
    def _seed(db_cursor, agent_id: str) -> None:
        db_cursor.execute(
            """
            INSERT INTO agent_runs
                (id, tenant_id, agent_id, trigger_type, trigger_detail, status,
                 total_cost_usd, duration_ms)
            VALUES
                (gen_random_uuid(), 'default', %s, 'cron', 'cron:daily',
                 'completed', 0.25, 1000),
                (gen_random_uuid(), 'default', %s, 'sub_agent', 'benchmark:s1:t1',
                 'timeout', 4.00, 2000),
                (gen_random_uuid(), 'default', %s, 'sub_agent', 'benchmark:s1:t2',
                 'completed', 2.00, 3000)
            """,
            (agent_id, agent_id, agent_id),
        )

    def test_enforce_excludes_benchmark_rows_and_reports_them_separately(
        self, db_cursor, db_conn, mock_get_connection, enforce_decontamination
    ):
        from robothor.engine.analytics import get_agent_stats

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        stats = get_agent_stats(agent_id, days=1, tenant_id="default")

        assert stats["total_runs"] == 1, (
            "benchmark runs with NULL parent are still counted as production runs"
        )
        assert stats["timeouts"] == 0, "a benchmark timeout was billed to the agent"
        assert float(stats["total_cost_usd"]) == pytest.approx(0.25)
        assert stats["benchmark_runs"] == 2, "benchmark traffic is not reported separately"
        assert float(stats["benchmark_cost_usd"]) == pytest.approx(6.00)

    def test_observe_keeps_legacy_numbers_but_measures_contamination(
        self, db_cursor, db_conn, mock_get_connection, observe_decontamination
    ):
        from robothor.engine.analytics import get_agent_stats

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        stats = get_agent_stats(agent_id, days=1, tenant_id="default")

        assert stats["total_runs"] == 3, "observe must not change the headline numbers"
        assert stats["benchmark_runs"] == 2
        assert stats["benchmark_excluded"] is False

    def test_fleet_health_excludes_benchmark_spend(
        self, db_cursor, db_conn, mock_get_connection, enforce_decontamination
    ):
        from robothor.engine.analytics import get_fleet_health

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        health = get_fleet_health(days=1, tenant_id="default")
        row = next((a for a in health["agents"] if a["agent_id"] == agent_id), None)

        assert row is not None
        assert row["total_runs"] == 1
        assert float(row["total_cost_usd"]) == pytest.approx(0.25)
        assert row["benchmark_runs"] == 2
        assert float(row["benchmark_cost_usd"]) == pytest.approx(6.00)


# ─── (c) benchmark runs must not file operator-facing CRM tasks ──────


class TestBenchmarkRunsFileNoCrmTasks:
    """6,887 "<Agent>: sub_agent run" tasks reached the operator queue.

    ``robothor/engine/tools/handlers/crm.py`` already refuses every
    task-mutating tool when ``ctx.is_benchmark``; the runner's own auto_task
    write bypasses the tool layer, so it never saw that guard.
    """

    @staticmethod
    def _config(*, is_benchmark: bool) -> AgentConfig:
        return AgentConfig(
            id="crm-dedup",
            name="CRM Dedup",
            model_primary="openrouter/test/model",
            timeout_seconds=30,
            delivery_mode=DeliveryMode.NONE,
            can_spawn_agents=False,
            planning_enabled=False,
            scratchpad_enabled=False,
            auto_task=True,
            is_benchmark=is_benchmark,
        )

    async def _execute(self, engine_config, config: AgentConfig) -> MagicMock:
        from robothor.engine.runner import AgentRunner

        create_task = MagicMock(return_value=str(uuid.uuid4()))
        with (
            patch("robothor.engine.runner.get_registry") as mock_reg,
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch"),
            patch("robothor.engine.tracking.create_steps_batch"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.crm.dal.create_task", create_task),
            patch("robothor.crm.dal.resolve_task"),
            patch("litellm.acompletion", side_effect=[_llm_response("done")]),
        ):
            registry = MagicMock()
            registry.build_for_agent.return_value = []
            registry.get_tool_names.return_value = []
            mock_reg.return_value = registry
            runner = AgentRunner(engine_config)
            runner.registry = registry
            await runner.execute(
                config.id,
                "benchmark task",
                trigger_type=TriggerType.SUB_AGENT,
                trigger_detail="benchmark:s1:t1",
                agent_config=config,
            )
        return create_task

    @pytest.mark.asyncio
    async def test_benchmark_run_files_no_task(self, engine_config):
        create_task = await self._execute(engine_config, self._config(is_benchmark=True))
        assert create_task.call_count == 0, (
            "a benchmark run filed an operator-facing CRM task — benchmark side "
            "effects must never enter the operator's queue"
        )

    @pytest.mark.asyncio
    async def test_normal_run_still_files_its_task(self, engine_config):
        create_task = await self._execute(engine_config, self._config(is_benchmark=False))
        assert create_task.call_count == 1, "auto_task regressed for production runs"


# ─── (d) drift guard: one filter, twelve call sites ──────────────────


class TestAnalyticsFilterParity:
    """Twelve hand-copied `parent_run_id IS NULL` clauses is how this drifted.

    Every ``agent_runs`` query in analytics.py must interpolate the shared
    helper so a thirteenth query cannot quietly ship the old predicate.
    """

    _SHARED = ("{prod_filter}", "{prod_filter_r}", "{no_bench}", "{bench_only}")

    @staticmethod
    def _source() -> str:
        from robothor.engine import analytics

        return Path(analytics.__file__).read_text()

    def test_no_hand_written_parent_run_id_predicate(self):
        src = self._source()
        assert src.count("parent_run_id IS NULL") == 1, (
            "the production-run predicate is hand-written in more than one "
            "place — that is exactly how benchmark traffic slipped in"
        )

    @staticmethod
    def _string_literals(tree: ast.AST) -> list[str]:
        """Every whole string literal, f-strings unparsed and docstrings dropped."""
        docstrings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        found: list[str] = []

        def visit(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                # Do NOT descend into an f-string: its literal fragments carry
                # the SQL but not the interpolated filter.
                if isinstance(child, ast.JoinedStr):
                    found.append(ast.unparse(child))
                elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                    if child not in docstrings:
                        found.append(child.value)
                else:
                    visit(child)

        visit(tree)
        return found

    def test_every_agent_runs_query_uses_the_shared_filter(self):
        tree = ast.parse(self._source())
        offenders: list[str] = []
        for text in self._string_literals(tree):
            if "agent_runs" not in text:
                continue
            if not any(token in text for token in self._SHARED):
                offenders.append(" ".join(text.split())[:90])
        assert not offenders, (
            "analytics queries touching agent_runs without the shared "
            f"production filter: {offenders}"
        )
