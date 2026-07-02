"""Batch relation insert fixes the entity-graph N+1 (Wave-2, W2-16).

The extract paths inserted one relation per round-trip in a loop. add_relations_batch
does one execute_values round-trip with the same upsert semantics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import robothor.memory.entities as entities


async def test_empty_rows_is_noop():
    assert await entities.add_relations_batch([]) == 0


async def test_batch_single_round_trip(monkeypatch):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = MagicMock()
    monkeypatch.setattr(entities, "get_connection", lambda: conn)

    rows = [(1, 2, "uses", None, 1.0), (3, 4, "works_at", 5, 0.9)]
    with patch("psycopg2.extras.execute_values") as ev:
        n = await entities.add_relations_batch(rows, tenant_id="t1")

    assert n == 2
    ev.assert_called_once()  # ONE round-trip for N relations
    sql = ev.call_args[0][1]
    assert "ON CONFLICT" in sql
    passed_rows = ev.call_args[0][2]
    assert passed_rows[0] == (1, 2, "uses", None, 1.0, "t1")
    assert passed_rows[1] == (3, 4, "works_at", 5, 0.9, "t1")
