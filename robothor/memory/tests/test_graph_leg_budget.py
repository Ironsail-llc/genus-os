"""The graph leg of search_facts has a time budget, enforced by the server.

A slow walk is stopped by Postgres (``statement_timeout`` armed on the leg's
own connection), the leg returns nothing and says so, and the other legs'
results stand. The limit is reset before the connection goes back, so it never
reaches another request; if the reset fails the connection is closed instead.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

TENANT = "test-tenant"
_VECTOR = [0.1] * 1024


@pytest.fixture
def db(scratch_db, monkeypatch):
    from robothor.db.migrate import _discover

    conn, dsn = scratch_db(through=list(_discover())[-1].migration_id)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (TENANT, TENANT),
        )
        cur.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) "
            "VALUES ('Alice Smith', 'person', %s)",
            (TENANT,),
        )
        cur.execute(
            "INSERT INTO memory_facts (fact_text, category, tenant_id, embedding) "
            "VALUES ('Alice Smith runs the depot', 'event', %s, %s::vector)",
            (TENANT, str(_VECTOR)),
        )

    held: list[Any] = []

    @contextmanager
    def _connect(*_a: Any, **_k: Any):
        c = psycopg2.connect(dsn)
        held.append(c)
        try:
            yield c
            if not c.closed:
                c.commit()
        finally:
            pass  # kept open so the test can inspect what would go back to a pool

    import robothor.db
    from robothor.llm import ollama
    from robothor.memory import facts

    for module in (robothor.db, facts):
        monkeypatch.setattr(module, "get_connection", _connect)
    monkeypatch.setattr(facts, "apply_hnsw_session", lambda _cur: None)
    # The graph leg is off by default (facts.GRAPH_LEG_DEFAULT); these tests
    # are about the leg, so they run with it on.
    monkeypatch.setattr(facts, "GRAPH_LEG_DEFAULT", True)

    async def _embed(_t: str, *_a: Any, **_k: Any) -> list[float]:
        return list(_VECTOR)

    monkeypatch.setattr(ollama, "get_embedding_async", _embed)
    monkeypatch.setattr(facts, "_embed_query", _embed)
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "0")
    yield held
    for c in held:
        if not c.closed:
            c.close()


def _slow_walk(monkeypatch, seconds: float = 3.0) -> None:
    """Make the walk a statement that runs far past any budget."""
    from robothor.memory import activation

    def slow(cur: Any, *_a: Any, **_k: Any) -> list[Any]:
        cur.execute("SELECT pg_sleep(%s)", (seconds,))
        return []

    monkeypatch.setattr(activation, "_query_graph", slow)


def test_the_budget_is_a_named_constant_of_250_ms() -> None:
    from robothor.memory import facts

    assert pytest.approx(0.25) == facts.GRAPH_LEG_BUDGET_SECONDS


async def test_a_slow_walk_is_cut_and_the_other_legs_still_answer(db, monkeypatch, caplog):
    from robothor.memory import facts

    _slow_walk(monkeypatch)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        rows = await facts.search_facts("Who runs the depot? Alice Smith", tenant_id=TENANT)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the walk was not cut at the budget ({elapsed:.2f}s)"
    assert [r["fact_text"] for r in rows] == ["Alice Smith runs the depot"]
    assert all("graph" not in (r.get("leg_ranks") or {}) for r in rows)
    assert any(
        "graph leg" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )


async def test_the_time_limit_never_goes_back_with_the_connection(db, monkeypatch) -> None:
    from robothor.memory import facts

    _slow_walk(monkeypatch)
    await facts.search_facts("Who runs the depot? Alice Smith", tenant_id=TENANT)
    returned = [c for c in db if not c.closed]
    assert returned, "the leg's connection went back open"
    for c in returned:
        with c.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] == "0"


def test_a_failed_reset_closes_the_connection_instead() -> None:
    """A connection still carrying the walk's limit must never be reused."""
    from robothor.memory import activation

    class _Cur:
        def __init__(self) -> None:
            self.connection = self
            self.closed = False

        def execute(self, sql: str, _p: Any = None) -> None:
            if sql.startswith("RESET"):
                raise RuntimeError("connection lost")

        def close(self) -> None:
            self.closed = True

    cur = _Cur()
    activation._reset_statement_timeout(cur)
    assert cur.closed
