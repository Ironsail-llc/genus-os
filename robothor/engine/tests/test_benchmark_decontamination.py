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
from typing import Any
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
            patch("robothor.engine.run_finalizer.create_steps_batch"),
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
    async def test_lineage_does_not_wait_for_the_flag(self, monkeypatch):
        """Linkage is a fact about the run, not a reporting rollout.

        This test used to assert the opposite — flag off, ``spawn_context``
        None — and the 2026-09-13 fleet audit is what that cost: the flag sat
        short of ``enforce``, so all 78 task runs recorded
        ``parent_run_id = NULL`` and the only way to ask which runs belonged to
        the night's benchmark was a time window over ``agent_runs``.

        What the flag still gates is how analytics REPORT benchmark traffic
        (``benchmark_excluded``); what it must never gate is whether the row
        says who spawned it.
        """
        monkeypatch.delenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED", raising=False)
        monkeypatch.delenv("ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE", raising=False)

        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=_make_mock_run())
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp"

        await _run_suite(mock_runner, _benchmark_child_config())

        spawn_context = mock_runner.execute.await_args.kwargs.get("spawn_context")
        assert spawn_context is not None
        assert spawn_context.parent_run_id == PARENT_RUN_ID


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
            patch("robothor.engine.run_finalizer.create_steps_batch"),
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


# ─── (e) benchmark spend survives the children moving tenant ────────────────


class TestBenchmarkSpendIsNotTenantScoped:
    """Every "benchmark traffic, reported separately" query used to read
    ``WHERE agent_id = %s AND tenant_id = %s AND <bench_only>`` with the
    PRODUCTION tenant bound. From 2026-09-13 the harness executes every task
    run as ``benchmark-sandbox``, so those rows match none of them and:

    * ``/costs`` reports ``benchmark_cost_usd = 0.0`` for every agent — the
      ~$30/month the break-out exists to surface simply disappears;
    * fleet health rolls the same zeros up;
    * ``analytics._report_contamination`` early-returns on
      ``benchmark_runs <= 0``, so the decontamination rollout's observe and
      alert rungs go permanently silent and the promotion evidence they exist
      to produce becomes unobtainable.

    ``bench_only`` already isolates benchmark rows by ``trigger_detail``, so
    the tenant predicate inside that branch bought nothing and now costs the
    measurement.
    """

    @staticmethod
    def _bench_query(fn: Any) -> str:
        """The SQL of the one query in ``fn`` that filters on ``bench_only``."""
        import inspect
        import re as _re

        src = inspect.getsource(fn)
        queries = _re.findall(r'f"""(.*?)"""', src, _re.DOTALL)
        matching = [q for q in queries if "{bench_only}" in q]
        assert matching, f"no bench_only query found in {fn.__name__}"
        assert len(matching) == 1, f"{fn.__name__} has {len(matching)} bench_only queries"
        return matching[0]

    def test_analytics_agent_stats_bench_query_is_tenant_agnostic(self):
        from robothor.engine.analytics import _benchmark_spend

        assert "tenant_id" not in self._bench_query(_benchmark_spend)

    def test_analytics_fleet_health_bench_query_is_tenant_agnostic(self):
        from robothor.engine.analytics import get_fleet_health

        assert "tenant_id" not in self._bench_query(get_fleet_health)

    def test_tracking_agent_stats_bench_query_is_tenant_agnostic(self):
        from robothor.engine.tracking import get_agent_stats

        assert "tenant_id" not in self._bench_query(get_agent_stats)

    def test_the_production_queries_keep_their_tenant_predicate(self):
        """Only the benchmark break-out loses it. Dropping it anywhere else
        would be a cross-tenant leak, not a fix."""
        import inspect

        from robothor.engine.analytics import get_agent_stats

        src = inspect.getsource(get_agent_stats)
        assert "AND tenant_id = %s" in src or "WHERE tenant_id = %s" in src

    def test_the_bench_query_is_unbound_from_the_rls_tenant(self):
        """A tenant-agnostic predicate is not enough when RLS is on: the
        policy filters the sandbox rows out before the WHERE clause is
        reached. Each bench query must relax the binding for its own
        transaction, or this whole fix is inert on an RLS instance."""
        import inspect

        from robothor.engine import analytics, tracking

        for fn in (
            analytics._benchmark_spend,
            analytics.get_fleet_health,
            tracking.get_agent_stats,
        ):
            src = inspect.getsource(fn)
            assert "read_every_tenant_in_transaction" in src, (
                f"{fn.__module__}.{fn.__name__} reads benchmark rows while still "
                "bound to one tenant — RLS hides the sandbox children"
            )


@pytest.mark.integration
class TestBenchmarkSpendCountsSandboxChildren:
    @staticmethod
    def _seed(db_cursor, agent_id: str) -> None:
        db_cursor.execute(
            """
            INSERT INTO crm_tenants (id, display_name, active)
            VALUES ('benchmark-sandbox', 'Benchmark Sandbox', TRUE)
            ON CONFLICT (id) DO NOTHING
            """
        )
        db_cursor.execute(
            """
            INSERT INTO agent_runs
                (id, tenant_id, agent_id, trigger_type, trigger_detail, status,
                 total_cost_usd, duration_ms)
            VALUES
                (gen_random_uuid(), 'default', %s, 'cron', 'cron:daily',
                 'completed', 0.25, 1000),
                (gen_random_uuid(), 'benchmark-sandbox', %s, 'sub_agent',
                 'benchmark:s1:t1', 'completed', 4.00, 2000),
                (gen_random_uuid(), 'benchmark-sandbox', %s, 'sub_agent',
                 'benchmark:s1:t2', 'completed', 2.00, 3000)
            """,
            (agent_id, agent_id, agent_id),
        )

    def test_analytics_sees_a_child_that_ran_in_the_sandbox(
        self, db_cursor, db_conn, mock_get_connection, observe_decontamination
    ):
        from robothor.engine.analytics import get_agent_stats

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        stats = get_agent_stats(agent_id, days=1, tenant_id="default")

        assert stats["benchmark_runs"] == 2, (
            "benchmark children that ran in the sandbox tenant are invisible to "
            "the spend break-out — $30/month of real money reported as zero"
        )
        assert float(stats["benchmark_cost_usd"]) == pytest.approx(6.00)

    def test_tracking_costs_surface_sees_them_too(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.tracking import get_agent_stats

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        stats = get_agent_stats(agent_id, hours=24, tenant_id="default")

        assert stats["benchmark_runs"] == 2
        assert float(stats["benchmark_cost_usd"]) == pytest.approx(6.00)

    def test_fleet_health_sees_them_too(
        self, db_cursor, db_conn, mock_get_connection, observe_decontamination
    ):
        from robothor.engine.analytics import get_fleet_health

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        health = get_fleet_health(days=1, tenant_id="default")
        row = next((a for a in health["agents"] if a["agent_id"] == agent_id), None)

        assert row is not None
        assert row["benchmark_runs"] == 2
        assert float(row["benchmark_cost_usd"]) == pytest.approx(6.00)

    def test_the_contamination_rung_still_reports(
        self, db_cursor, db_conn, mock_get_connection, observe_decontamination, caplog
    ):
        """``observe`` exists to produce the promotion evidence. A count that
        can only be zero is a rung that never reports."""
        import logging

        from robothor.engine.analytics import get_agent_stats

        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
        self._seed(db_cursor, agent_id)

        with caplog.at_level(logging.WARNING, logger="robothor.engine.analytics"):
            get_agent_stats(agent_id, days=1, tenant_id="default")

        assert any("benchmark contamination" in r.getMessage() for r in caplog.records), (
            "the decontamination observe rung went silent when the children moved tenant"
        )


# ─── (f) the runbook's own audit queries, run against an RLS fake ───────────


class TestTheRunbookAuditQueriesActuallyWork:
    """The verification a change ships to prove itself has to be probed too.

    The lineage SQL self-joins ``agent_runs``: the parent lives in the owning
    tenant, the children in ``benchmark-sandbox``. Migration 081 puts a
    ``tenant_isolation`` policy on every table with a ``tenant_id`` column,
    ``FORCE ROW LEVEL SECURITY`` included, and it applies to each *reference* in
    a query — both aliases of a self-join. So from a connection bound to the
    owning tenant the join drops every sandbox child, and "expect
    benchmark-sandbox only" is unreachable: a clean night and a blind query both
    return nothing.

    These tests execute the queries **as written in the runbook** against a
    sqlite fake of that policy: ``agent_runs`` is a view over the real rows,
    filtered exactly the way the policy filters (permissive when the binding is
    empty). One declared normalisation — Postgres' ``now() - interval '1 day'``
    is rewritten to a fixed timestamp — because what is under test is the
    visibility of the join, not date arithmetic.
    """

    RUNBOOK = Path(__file__).resolve().parents[3] / "docs" / "runbooks" / "BENCHMARK_SANDBOX.md"

    @classmethod
    def _runbook_query(cls, marker: str) -> str:
        """The fenced ``sql`` block whose first line names ``marker``."""
        import re as _re

        blocks = _re.findall(r"```sql\n(.*?)```", cls.RUNBOOK.read_text(), _re.DOTALL)
        matching = [b for b in blocks if marker in b.splitlines()[0]]
        assert matching, f"no ```sql block in the runbook starts with {marker!r}"
        assert len(matching) == 1, f"{len(matching)} blocks start with {marker!r}"
        return matching[0].replace("now() - interval '1 day'", "'2000-01-01'")

    @staticmethod
    def _db(binding: str):
        """A sqlite fake of migration 081's policy on ``agent_runs``.

        One parent in the owning tenant, three children in the sandbox, and one
        child deliberately leaked into the owning tenant.
        """
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE agent_runs_storage ("
            " id TEXT, tenant_id TEXT, agent_id TEXT, parent_run_id TEXT,"
            " trigger_detail TEXT, started_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_runs_storage VALUES (?,?,?,?,?,?)",
            [
                ("p1", "acme-instance", "benchmark-runner", None, "cron:daily", "2026-09-13"),
                ("c1", "benchmark-sandbox", "email-analyst", "p1", "benchmark:s1:t1", "2026-09-13"),
                ("c2", "benchmark-sandbox", "email-analyst", "p1", "benchmark:s1:t2", "2026-09-13"),
                ("c3", "benchmark-sandbox", "crm-dedup", "p1", "benchmark:s2:t1", "2026-09-13"),
                ("c4", "acme-instance", "crm-hygiene", "p1", "benchmark:s3:t1", "2026-09-13"),
            ],
        )
        # The policy: permissive on an empty binding, otherwise equality.
        where = "1=1" if binding == "" else f"tenant_id = '{binding}'"
        conn.execute(f"CREATE VIEW agent_runs AS SELECT * FROM agent_runs_storage WHERE {where}")
        return conn

    def test_unbound_the_audit_sees_every_child_and_names_the_leak(self):
        rows = self._db("").execute(self._runbook_query("-- UNBOUND")).fetchall()
        by_tenant = {(agent, tenant): n for agent, tenant, n in rows}
        assert by_tenant.get(("email-analyst", "benchmark-sandbox")) == 2
        assert by_tenant.get(("crm-dedup", "benchmark-sandbox")) == 1
        assert by_tenant.get(("crm-hygiene", "acme-instance")) == 1, (
            "the unbound audit did not surface the leaked child — it is the only "
            "form that can, and the runbook sends the operator here"
        )

    def test_bound_to_the_owning_tenant_a_clean_night_is_empty(self):
        """And the query the runbook gives for that binding says so."""
        rows = self._db("acme-instance").execute(self._runbook_query("-- BOUND")).fetchall()
        assert [r for r in rows if r[1] == "benchmark-sandbox"] == [], (
            "a sandbox child was visible from a bound connection — the fake's "
            "policy is not filtering, so this test proves nothing"
        )
        assert rows == [("crm-hygiene", "acme-instance", 1)], (
            "the bound query must return exactly the leak, and nothing else"
        )

    def test_the_unbound_query_bound_would_hide_the_children(self):
        """Why the runbook's two forms are not interchangeable: run the UNBOUND
        query on a bound connection and the sandbox children vanish, which reads
        identically to 'the benchmark never ran'."""
        rows = self._db("acme-instance").execute(self._runbook_query("-- UNBOUND")).fetchall()
        assert all(tenant != "benchmark-sandbox" for _, tenant, _ in rows)

    def test_bound_to_the_sandbox_the_join_drops_everything(self):
        """The parent is filtered out, so the JOIN returns nothing at all —
        the third way to get an empty result that means nothing."""
        rows = self._db("benchmark-sandbox").execute(self._runbook_query("-- UNBOUND")).fetchall()
        assert rows == []
