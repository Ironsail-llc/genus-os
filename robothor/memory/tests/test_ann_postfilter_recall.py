"""An ANN index whose graph is mostly dead rows silently loses results.

2026-08-27. ``memory_facts`` carried TWO hnsw indexes on the same
``embedding`` column: a full one (1,085 MB) and a partial one over
``WHERE is_active`` (306 MB). 82.4% of the table is superseded, so the full
graph is 82% dead rows — and the planner preferred it.

Measured on production, same query, LIMIT 20:

    idx_facts_embedding         -> 9 rows,  "Rows Removed by Filter: 31"
    idx_facts_embedding_active  -> 20 rows, no filtering

This is a RECALL bug, not a tuning preference. pgvector's index scan walks
the graph and the ``is_active`` predicate is applied afterwards, so a LIMIT
is consumed by candidates that are then discarded. Memory search was
returning 55% fewer facts than asked for, fleet-wide, and nothing surfaced
it because a short result set looks exactly like a sparse corpus.

Every embedding query against this table filters ``is_active``
(``facts.py:928``, ``active_only: bool = True``, and no caller in the tree
passes False), so the full index served nothing while costing hnsw
maintenance on every write.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402


def _conn():
    db = os.environ.get("ROBOTHOR_TEST_DB", "robothor_memory")
    try:
        return psycopg2.connect(dbname=db)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no database available: {exc}")


def _hnsw_indexes_on(cur, table: str, column: str) -> list[str]:
    cur.execute(
        """
        SELECT indexname, indexdef FROM pg_indexes
        WHERE tablename = %s AND indexdef ILIKE '%%hnsw%%'
        """,
        (table,),
    )
    return [name for name, ddl in cur.fetchall() if column in ddl]


def test_no_unfiltered_ann_index_shadows_the_active_one():
    """Two hnsw indexes on one column means the planner can pick the bad one."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.memory_facts')")
        if cur.fetchone()[0] is None:
            pytest.skip("memory_facts not present in this database")

        found = _hnsw_indexes_on(cur, "memory_facts", "embedding")
        unfiltered = []
        for name in found:
            cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,))
            ddl = cur.fetchone()[0]
            if "WHERE" not in ddl.upper():
                unfiltered.append(name)

        assert not (len(found) > 1 and unfiltered), (
            "memory_facts has an unfiltered hnsw index alongside a partial one: "
            f"{unfiltered}. Every search filters is_active, so the unfiltered "
            "graph only costs recall — its candidates are post-filtered away "
            "and the LIMIT comes back short (measured 9/20 on production)."
        )
    finally:
        conn.close()
