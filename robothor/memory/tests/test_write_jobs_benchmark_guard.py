"""A benchmark run may not write memory into a tenant that is not the sandbox.

Incident 2026-09-12. With the benchmark sandbox ``off``, the harness ran its
sub-runs under the instance's own tenant. The CRM deny-set held; the memory
path did not, and a suite fixture's fictional person became durable memory
facts that a real agent later read back as established fact and acted on.

The fix is a boundary, not another call site: every durable memory write asks
:mod:`robothor.engine.run_context` whether the run it is inside is allowed to
write this tenant. These tests pin that boundary at the two functions every
memory write funnels through.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

import robothor.memory.facts as facts_mod
import robothor.memory.write_jobs as write_jobs
from robothor.engine.run_context import benchmark_run_scope

_SANDBOX = "benchmark-sandbox"
_PRODUCTION = "an-instance-tenant"
_SECRET = "Bob Quill bob.quill@example.com prefers mornings"


@pytest.fixture
def fake_connection(monkeypatch: Any) -> MagicMock:
    """A ``get_connection`` whose cursor records every statement."""
    cur = MagicMock()
    cur.fetchone.return_value = (7,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda self: self  # type: ignore[assignment]
    conn.__exit__ = lambda self, *a: False  # type: ignore[assignment]

    class _Ctx:
        def __enter__(self) -> MagicMock:
            return conn

        def __exit__(self, *_a: Any) -> bool:
            return False

    monkeypatch.setattr(write_jobs, "get_connection", lambda: _Ctx())
    conn.recorded_cursor = cur  # type: ignore[attr-defined]
    return conn


class TestEnqueueWrite:
    @pytest.mark.asyncio
    async def test_benchmark_run_writing_a_production_tenant_is_refused(
        self, fake_connection: MagicMock, caplog: Any
    ) -> None:
        with (
            caplog.at_level(logging.WARNING, logger="robothor.engine.run_context"),
            benchmark_run_scope(True, agent_id="agent-under-test", run_id="run-1"),
        ):
            job_id = await write_jobs.enqueue_write(_SECRET, tenant_id=_PRODUCTION)

        assert job_id is None
        assert fake_connection.cursor.call_count == 0, "the refused write still hit the database"

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "agent-under-test" in message
        assert "run-1" in message
        assert _PRODUCTION in message
        assert "Bob Quill" not in message
        assert "bob.quill@example.com" not in message

    @pytest.mark.asyncio
    async def test_benchmark_run_writing_the_sandbox_tenant_is_allowed(
        self, fake_connection: MagicMock
    ) -> None:
        with benchmark_run_scope(True, agent_id="agent-under-test", run_id="run-1"):
            job_id = await write_jobs.enqueue_write(_SECRET, tenant_id=_SANDBOX)
        assert job_id == 7
        assert fake_connection.cursor.call_count == 1

    @pytest.mark.asyncio
    async def test_outside_a_benchmark_run_nothing_changes(
        self, fake_connection: MagicMock
    ) -> None:
        job_id = await write_jobs.enqueue_write(_SECRET, tenant_id=_PRODUCTION)
        assert job_id == 7
        assert fake_connection.cursor.call_count == 1

    @pytest.mark.asyncio
    async def test_the_scope_is_restored_on_exit(self, fake_connection: MagicMock) -> None:
        """A leaked flag would block the benchmark RUNNER's own writes."""
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            pass
        assert await write_jobs.enqueue_write(_SECRET, tenant_id=_PRODUCTION) == 7


class TestStoreFactsBatch:
    """The default path. ``MEMORY_ASYNC_WRITE`` is off, so ``store_memory``
    never reaches ``enqueue_write`` — guarding only the queue would have left
    the path that actually caused the incident wide open."""

    @pytest.mark.asyncio
    async def test_benchmark_run_writing_a_production_tenant_stores_nothing(
        self, monkeypatch: Any
    ) -> None:
        called = False

        def _boom() -> Any:
            nonlocal called
            called = True
            raise AssertionError("store_facts_batch opened a connection")

        monkeypatch.setattr(facts_mod, "get_connection", _boom)
        fact = {"fact_text": _SECRET, "category": "personal", "entities": []}
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            ids = await facts_mod.store_facts_batch(
                [fact], _SECRET, "conversation", tenant_id=_PRODUCTION
            )
        assert ids == []
        assert called is False

    @pytest.mark.asyncio
    async def test_benchmark_run_writing_the_sandbox_tenant_still_stores(
        self, monkeypatch: Any
    ) -> None:
        seen: list[str] = []

        class _Ctx:
            def __enter__(self) -> MagicMock:
                conn = MagicMock()
                conn.cursor.return_value = MagicMock()
                return conn

            def __exit__(self, *_a: Any) -> bool:
                return False

        async def _embeddings(texts: list[str]) -> list[list[float]]:
            return [[0.0] for _ in texts]

        monkeypatch.setattr(facts_mod, "get_connection", lambda: _Ctx())
        monkeypatch.setattr(
            facts_mod.llm_client, "get_embeddings_batch_async", _embeddings, raising=False
        )
        monkeypatch.setattr(
            facts_mod,
            "_insert_fact",
            lambda cur, params, *, tenant_id, content_hash: seen.append(tenant_id) or 1,
        )
        fact = {"fact_text": "a sandbox fact", "category": "personal", "entities": []}
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            ids = await facts_mod.store_facts_batch(
                [fact], "a sandbox fact", "conversation", tenant_id=_SANDBOX
            )
        assert ids == [1]
        assert seen == [_SANDBOX]


class TestFactAccessLog:
    """``search_memory`` is allowed to a benchmark child in BOTH modes, and it
    writes a ``fact_access_log`` row per consulted fact.

    Those rows are the only input to ``fact_access_rollup`` and so to the
    memory decay scorer, and they are what ``bump_failure_for_run`` joins
    against to decrement ``confidence`` on real facts. A read tool that writes
    is still a write, and the runbook's headline rule — "a benchmark run may
    write exactly one tenant" — was false while this one was unguarded.
    """

    def test_a_benchmark_child_logs_no_access_against_a_production_tenant(
        self, monkeypatch: Any
    ) -> None:
        """Counted, not raised. ``log_fact_access`` swallows every exception by
        contract, so a probe that raises inside it proves nothing — it would
        pass against a completely unguarded function."""
        import robothor.memory.outcomes as outcomes

        opened = 0

        class _Ctx:
            def __enter__(self) -> MagicMock:
                nonlocal opened
                opened += 1
                conn = MagicMock()
                conn.cursor.return_value = MagicMock()
                return conn

            def __exit__(self, *_a: Any) -> bool:
                return False

        monkeypatch.setattr(outcomes, "get_connection", lambda: _Ctx())
        monkeypatch.setattr("psycopg2.extras.execute_values", lambda *a, **kw: None)
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            outcomes.log_fact_access("run-1", [1, 2, 3], tenant_id=_PRODUCTION)
        assert opened == 0, "the refused access log still reached the database"

    def test_it_never_raises_into_the_read_that_called_it(self) -> None:
        """log_fact_access is best-effort by contract: search_memory must
        return its results whatever the logger does."""
        import robothor.memory.outcomes as outcomes

        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            assert outcomes.log_fact_access("run-1", [1], tenant_id=_PRODUCTION) is None

    def test_the_sandbox_tenant_still_logs(self, monkeypatch: Any) -> None:
        import robothor.memory.outcomes as outcomes

        opened = False

        class _Ctx:
            def __enter__(self) -> MagicMock:
                nonlocal opened
                opened = True
                conn = MagicMock()
                conn.cursor.return_value = MagicMock()
                return conn

            def __exit__(self, *_a: Any) -> bool:
                return False

        monkeypatch.setattr(outcomes, "get_connection", lambda: _Ctx())
        monkeypatch.setattr("psycopg2.extras.execute_values", lambda *a, **kw: None)
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            outcomes.log_fact_access("run-1", [1], tenant_id=_SANDBOX)
        assert opened is True

    def test_bump_failure_for_run_is_guarded_too(self, monkeypatch: Any) -> None:
        """It UPDATEs memory_facts.confidence on production rows, keyed off the
        access log — the more damaging half of the same pair."""
        import robothor.memory.outcomes as outcomes

        def _no_database() -> Any:
            raise AssertionError("bump_failure_for_run opened a connection")

        monkeypatch.setattr(outcomes, "get_connection", _no_database)
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            result = outcomes.bump_failure_for_run("run-1", tenant_id=_PRODUCTION)
        assert result == {"facts_touched": 0, "facts_confidence_dropped": 0}

    def test_outside_a_benchmark_run_both_still_write(self, monkeypatch: Any) -> None:
        import robothor.memory.outcomes as outcomes

        opened = 0

        class _Ctx:
            def __enter__(self) -> MagicMock:
                nonlocal opened
                opened += 1
                conn = MagicMock()
                cur = MagicMock()
                cur.rowcount = 2
                conn.cursor.return_value = cur
                return conn

            def __exit__(self, *_a: Any) -> bool:
                return False

        monkeypatch.setattr(outcomes, "get_connection", lambda: _Ctx())
        monkeypatch.setattr("psycopg2.extras.execute_values", lambda *a, **kw: None)
        outcomes.log_fact_access("run-1", [1], tenant_id=_PRODUCTION)
        assert outcomes.bump_failure_for_run("run-1", tenant_id=_PRODUCTION)["facts_touched"] == 2
        assert opened == 2
