"""A benchmark_results INSERT must never reach a non-test database from pytest.

709 synthetic rows (agent ``main``, suites ``s1`` / ``s2`` / ``test-suite``,
``triggered_by='manual'``) accumulated in the production ``benchmark_results``
table between 2026-05-11 and 2026-08-19 — 36 of them inside a single second on
the final day, the signature of one ``pytest robothor/engine/tests/test_benchmark.py``
run. Because ``goals.py`` and the Telegram ``/goals`` command read the *latest*
row for an agent with no suite filter, main's displayed score read 100% for
about 15 hours against a real 64%.

The leak path was the isolation fixture in ``test_benchmark.py``: it patched
``robothor.crm.dal.get_connection``, but ``_benchmark_run`` imports
``robothor.db.connection.get_connection`` inside the function body, so the
patch never intercepted the write. (``crm.dal`` merely re-exports the same
object; rebinding the re-export leaves the original untouched.)

The generic pin — root ``conftest.py`` forcing ``ROBOTHOR_DB_NAME`` and
``assert_test_database()`` in the pool factory — landed 2026-08-20, one day
after the last polluted row, and still has two holes this guard closes:

* ``get_pool()`` only asserts when it *creates* the pool; a pool warmed before
  the first test is reused forever without re-checking.
* ``assert_test_database()`` keys off ``PYTEST_CURRENT_TEST``, which is unset
  during collection and session-scoped fixtures.
* Both benchmark writers swallow ``Exception`` around the INSERT, so a guard
  that raises a plain ``RuntimeError`` is downgraded to a log line nobody reads.

These tests pin the write-time guard. None of them opens a real connection.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.db.connection as conn_mod
from robothor.db.connection import DatabaseGuardError, assert_test_database_write
from robothor.engine.tools.dispatch import ToolContext

CTX = ToolContext(agent_id="auto-agent", workspace="/tmp/test-workspace")

PROD_DB = "robothor_memory"
TEST_DB = "robothor_test"


# ─── Fakes ──────────────────────────────────────────────────────────


class _RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def execute(self, sql: str, params: Any = None) -> None:
        self._statements.append(sql)

    def fetchone(self) -> Any:
        return None

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RecordingConn:
    """A connection that reports which database it would write to."""

    def __init__(self, dbname: str, statements: list[str]) -> None:
        self._dbname = dbname
        self.statements = statements
        self.committed = False

    def get_dsn_parameters(self) -> dict[str, str]:
        return {"dbname": self._dbname}

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.statements)

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> _RecordingConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_connection(dbname: str) -> tuple[Any, list[str]]:
    statements: list[str] = []

    def factory(*a: object, **kw: object) -> _RecordingConn:
        return _RecordingConn(dbname, statements)

    return factory, statements


def _mock_blocks() -> tuple[dict[str, str], Any, Any]:
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-04-03T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _seeded_suite(store: dict[str, str]) -> None:
    store["benchmark:main:test-suite"] = json.dumps(
        {
            "id": "test-suite",
            "agent_id": "main",
            "max_cost_usd": 1.0,
            "tasks": [
                {
                    "id": "t1",
                    "prompt": "Check calendar",
                    "category": "correctness",
                    "weight": 1.0,
                    "expected": {"must_contain": ["calendar"]},
                },
            ],
        }
    )


def _mock_runner() -> Any:
    run = MagicMock()
    run.output_text = "The calendar shows events tomorrow"
    run.total_cost_usd = 0.05
    run.steps = [MagicMock()] * 3
    run.status = MagicMock(value="completed")
    run.id = "run-123"
    run.input_tokens = 100
    run.output_tokens = 50
    run.error_message = None

    runner = MagicMock()
    runner.execute = AsyncMock(return_value=run)
    runner.config = MagicMock()
    runner.config.manifest_dir = "/tmp"
    return runner


async def _run_benchmark(connection_factory: Any) -> dict[str, Any]:
    from robothor.engine.tools.handlers.benchmark import _benchmark_run

    store, read_fn, write_fn = _mock_blocks()
    _seeded_suite(store)

    agent_config = MagicMock()
    agent_config.max_iterations = 10
    agent_config.cost_budget_usd = 1.0

    with (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        patch("robothor.db.connection.get_connection", connection_factory),
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=_mock_runner()),
        patch("robothor.engine.config.load_agent_config", return_value=agent_config),
    ):
        return await _benchmark_run(
            {"agent_id": "main", "suite_id": "test-suite", "tag": "baseline"},
            CTX,
        )


# ─── The guard itself ───────────────────────────────────────────────


class TestAssertTestDatabaseWrite:
    def test_refuses_production_database(self):
        with pytest.raises(DatabaseGuardError) as exc:
            assert_test_database_write(PROD_DB, "benchmark_results")
        message = str(exc.value)
        assert PROD_DB in message, "the message must name the offending database"
        assert "benchmark_results" in message, "the message must name the table"
        assert "ROBOTHOR_TEST_DB_ALLOW" in message, "the message must name the escape hatch"

    def test_allows_test_suffixed_database(self):
        assert_test_database_write(TEST_DB, "benchmark_results")  # must not raise

    def test_escape_hatch_allows_exactly_one_name(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TEST_DB_ALLOW", "robothor_release_gate")
        assert_test_database_write("robothor_release_gate", "benchmark_results")
        with pytest.raises(DatabaseGuardError):
            assert_test_database_write(PROD_DB, "benchmark_results")

    def test_noop_outside_pytest(self, monkeypatch):
        monkeypatch.setattr(conn_mod, "in_pytest", lambda: False)
        assert_test_database_write(PROD_DB, "benchmark_results")  # production is fine live

    def test_is_a_runtime_error(self):
        """Existing `pytest.raises(RuntimeError)` call sites must keep working."""
        assert issubclass(DatabaseGuardError, RuntimeError)

    def test_in_pytest_is_true_here(self):
        """Detection must not depend on PYTEST_CURRENT_TEST alone.

        That variable is unset during collection and session-scoped fixtures —
        exactly when a stray module-level write would slip past.
        """
        monkeypatched = pytest.MonkeyPatch()
        monkeypatched.delenv("PYTEST_CURRENT_TEST", raising=False)
        try:
            assert conn_mod.in_pytest() is True
        finally:
            monkeypatched.undo()


class TestConnectionDatabaseName:
    def test_reads_the_live_connection(self):
        conn = _RecordingConn(PROD_DB, [])
        assert conn_mod.connection_database_name(conn) == PROD_DB

    def test_falls_back_to_config_when_unavailable(self):
        """A pooled connection that cannot report its DSN must not read as safe."""

        class _Opaque:
            pass

        name = conn_mod.connection_database_name(_Opaque())
        assert name == conn_mod.get_config().db.name


# ─── The benchmark write-through ────────────────────────────────────


class TestBenchmarkRunWriteGuard:
    @pytest.mark.asyncio
    async def test_refuses_to_insert_into_production(self):
        factory, statements = _fake_connection(PROD_DB)
        with pytest.raises(DatabaseGuardError) as exc:
            await _run_benchmark(factory)
        assert PROD_DB in str(exc.value)
        assert not any("INSERT INTO benchmark_results" in s for s in statements), (
            "the guard must fire BEFORE the INSERT is executed"
        )

    @pytest.mark.asyncio
    async def test_writes_to_a_test_database(self):
        """Negative control: the legitimate test-DB path still writes."""
        factory, statements = _fake_connection(TEST_DB)
        result = await _run_benchmark(factory)
        assert result["success"] is True
        assert any("INSERT INTO benchmark_results" in s for s in statements)

    @pytest.mark.asyncio
    async def test_guard_is_not_swallowed_by_the_best_effort_handler(self):
        """The write is wrapped in `except Exception: logger.warning(...)`.

        A guard that gets downgraded to a log line is not a guard — the run
        would report success while the row landed in production.
        """
        factory, _ = _fake_connection(PROD_DB)
        with pytest.raises(DatabaseGuardError):
            await _run_benchmark(factory)

    @pytest.mark.asyncio
    async def test_patching_crm_dal_does_not_intercept_the_write(self):
        """The historical leak: `crm.dal.get_connection` is the wrong seam.

        `_benchmark_run` imports `robothor.db.connection.get_connection` inside
        the function body, so rebinding the `crm.dal` re-export is decorative.
        If this ever starts passing without the guard, the fixture in
        test_benchmark.py has been silently re-broken.
        """
        import robothor.crm.dal as dal_mod

        dal_factory, dal_statements = _fake_connection(TEST_DB)
        prod_factory, _ = _fake_connection(PROD_DB)
        with patch.object(dal_mod, "get_connection", dal_factory):
            with pytest.raises(DatabaseGuardError):
                await _run_benchmark(prod_factory)
        assert dal_statements == [], "the crm.dal patch never sees the benchmark write"


# ─── The memory-eval write path ─────────────────────────────────────


class TestRecordBenchmarkRowGuard:
    def _row(self) -> dict[str, Any]:
        return {
            "agent_id": "memory",
            "suite_id": "memory-eval",
            "suite_path": None,
            "total_cases": 12,
            "passed": 12,
            "failed": 0,
            "pass_rate": 1.0,
            "category_scores": {},
            "failures": [],
            "triggered_by": "manual",
            "experiment_id": None,
            "cost_usd": 0.0,
        }

    def test_refuses_to_insert_into_production(self):
        from robothor.memory.eval import record_benchmark_row

        factory, statements = _fake_connection(PROD_DB)
        with patch("robothor.db.connection.get_connection", factory):
            with pytest.raises(DatabaseGuardError) as exc:
                record_benchmark_row(self._row())
        assert PROD_DB in str(exc.value)
        assert not any("INSERT INTO benchmark_results" in s for s in statements)

    def test_writes_to_a_test_database(self):
        from robothor.memory.eval import record_benchmark_row

        factory, statements = _fake_connection(TEST_DB)
        with patch("robothor.db.connection.get_connection", factory):
            assert record_benchmark_row(self._row()) is True
        assert any("INSERT INTO benchmark_results" in s for s in statements)
