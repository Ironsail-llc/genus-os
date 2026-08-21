"""Junk entity names must never poison a relation batch.

``memory_relations`` has FK constraints to ``memory_entities(id)`` and
:func:`add_relations_batch` inserts every row in a SINGLE ``execute_values``
statement, so Postgres rejects the WHOLE statement when one row carries an id
that does not exist. The exception is then swallowed by a broad
``except Exception`` in ``robothor.memory.ingestion``, so a single junk entity
silently costs an entire batch of relations.

These tests pin the two halves of the fix:
  * :func:`upsert_entity` returns ``None`` (never a truthy sentinel) for a name
    that cannot be a real entity, and callers filter on ``is not None``.
  * :func:`add_relations_batch` survives a bad row — it drops it, logs at
    WARNING with a count, and still stores every valid relation.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

import robothor.memory.entities as entities

UUID_NAME = "3f7c1e9a-2b4d-4c6e-8a10-9d5f2c7b1e34"


def _run(coro: Any) -> Any:
    """Run a coroutine on a fresh loop (matches test_tenant_isolation.py)."""
    return asyncio.run(coro)


def _mock_conn(cursor: MagicMock) -> MagicMock:
    """Build a ``get_connection()`` context-manager mock around ``cursor``."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


def _id_cursor(fact_rows: list[dict[str, Any]] | None = None) -> MagicMock:
    """Cursor that hands out a fresh entity id for every ``RETURNING id``."""
    cur = MagicMock()
    counter = itertools.count(101)
    cur.fetchone.side_effect = lambda: (next(counter),)
    cur.fetchall.return_value = fact_rows or []
    return cur


def _entity_names_inserted(cur: MagicMock) -> list[str]:
    return [
        call[0][1][0]
        for call in cur.execute.call_args_list
        if "memory_entities" in call[0][0] and len(call[0]) > 1
    ]


class _FKEnforcingDB:
    """Stand-in for the FK constraint on ``memory_relations``.

    Postgres rejects the ENTIRE ``execute_values`` statement when any row
    violates the foreign key to ``memory_entities(id)`` — that batch-wide
    failure is exactly what the hardening has to survive, so the fake
    reproduces it rather than checking rows one at a time.
    """

    def __init__(self, known_ids: set[int]) -> None:
        self.known_ids = known_ids
        self.inserted: list[tuple[Any, ...]] = []

    def execute_values(self, cur: Any, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if any(r[0] not in self.known_ids or r[1] not in self.known_ids for r in rows):
            raise psycopg2.errors.ForeignKeyViolation(
                'insert or update on table "memory_relations" violates foreign '
                'key constraint "memory_relations_source_entity_id_fkey"'
            )
        self.inserted.extend(rows)


# ─── upsert_entity: junk names return None ───────────────────────────────


@patch("robothor.memory.entities.get_connection")
def test_upsert_entity_returns_none_for_uuid_shaped_name(mock_get_conn: MagicMock) -> None:
    """A UUID echoed back as an entity name is junk — no row, no id."""
    cur = _id_cursor()
    mock_get_conn.return_value = _mock_conn(cur)

    assert _run(entities.upsert_entity(UUID_NAME, "project", tenant_id="tenant_x")) is None
    assert cur.execute.call_count == 0, "junk name must not reach the database"


@pytest.mark.parametrize("name", ["", "   ", "x", " y "])
@patch("robothor.memory.entities.get_connection")
def test_upsert_entity_returns_none_for_empty_or_single_char(
    mock_get_conn: MagicMock, name: str
) -> None:
    """Empty, whitespace-only and one-character names are junk."""
    cur = _id_cursor()
    mock_get_conn.return_value = _mock_conn(cur)

    assert _run(entities.upsert_entity(name, "person", tenant_id="tenant_x")) is None
    assert cur.execute.call_count == 0


@pytest.mark.parametrize("name", ["AI", "R2", "Acme Corp", "3f7c1e9a-2b4d"])
@patch("robothor.memory.entities.get_connection")
def test_upsert_entity_keeps_short_real_names(mock_get_conn: MagicMock, name: str) -> None:
    """The guard stays conservative: two characters is a real entity, and a
    partial hex string is not a UUID."""
    cur = _id_cursor()
    mock_get_conn.return_value = _mock_conn(cur)

    assert _run(entities.upsert_entity(name, "technology", tenant_id="tenant_x")) == 101


# ─── add_relations_batch: one bad row cannot kill the batch ──────────────


def test_one_bad_row_does_not_kill_the_relation_batch(caplog: pytest.LogCaptureFixture) -> None:
    """HEADLINE: a stale entity id used to cost the WHOLE batch.

    Row 2 references entity 999, which does not exist. Postgres rejects the
    single statement, so before the fix all three relations were lost (and the
    ForeignKeyViolation was swallowed upstream by ingestion.py). The valid rows
    must survive.
    """
    cur = MagicMock()
    db = _FKEnforcingDB(known_ids={0, 1, 2, 3, 4})
    rows = [
        (1, 2, "uses", None, 1.0),
        (999, 3, "works_at", None, 1.0),
        (3, 4, "manages", 7, 0.9),
        (0, 1, "built_with", None, 1.0),  # id 0 is a valid id, not a falsy sentinel
    ]

    with (
        patch.object(entities, "get_connection", lambda: _mock_conn(cur)),
        patch("psycopg2.extras.execute_values", db.execute_values),
        caplog.at_level(logging.WARNING, logger="robothor.memory.entities"),
    ):
        stored = _run(entities.add_relations_batch(rows, tenant_id="t1"))

    assert stored == 3
    assert [(r[0], r[1], r[2]) for r in db.inserted] == [
        (1, 2, "uses"),
        (3, 4, "manages"),
        (0, 1, "built_with"),
    ]
    assert "999" not in [str(r[0]) for r in db.inserted]
    assert caplog.records, "a dropped relation must be logged at WARNING"


def test_relation_batch_drops_invalid_ids_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """None / negative-sentinel ids never reach Postgres, and the drop is loud."""
    cur = MagicMock()
    db = _FKEnforcingDB(known_ids={0, 1, 2, 3})
    rows = [
        (1, 2, "uses", None, 1.0),
        (None, 2, "works_at", None, 1.0),
        (1, None, "works_at", None, 1.0),
        (-1, 3, "manages", None, 1.0),  # the truthy sentinel this fix replaces
        (0, 3, "knows", None, 1.0),
    ]

    with (
        patch.object(entities, "get_connection", lambda: _mock_conn(cur)),
        patch("psycopg2.extras.execute_values", db.execute_values),
        caplog.at_level(logging.WARNING, logger="robothor.memory.entities"),
    ):
        stored = _run(entities.add_relations_batch(rows, tenant_id="t1"))

    assert stored == 2
    assert [(r[0], r[1]) for r in db.inserted] == [(1, 2), (0, 3)]
    warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "3" in warnings, f"dropped-row count missing from WARNING log: {warnings!r}"


# ─── extract paths: junk entities never become relation endpoints ────────


def _extraction_payload() -> dict[str, Any]:
    return {
        "entities": [
            {"name": "Alice", "type": "person"},
            {"name": UUID_NAME, "type": "project"},
            {"name": "Acme", "type": "organization"},
        ],
        "relations": [
            {"source": "Alice", "target": "Acme", "relation": "works_at"},
            {"source": "Alice", "target": UUID_NAME, "relation": "uses"},
        ],
    }


def test_extract_batch_never_passes_none_to_add_relations_batch() -> None:
    """Spy on the batch insert: every id is a real stored entity id."""
    cur = _id_cursor(fact_rows=[{"id": 5, "fact_text": "Alice works at Acme"}])
    seen: list[list[tuple[Any, ...]]] = []

    async def _fake_extract(_text: str) -> dict[str, Any]:
        return _extraction_payload()

    async def _spy_batch(rows: list[tuple[Any, ...]], **kwargs: Any) -> int:
        seen.append(rows)
        return len(rows)

    with (
        patch.object(entities, "get_connection", lambda **kw: _mock_conn(cur)),
        patch.object(entities, "extract_entities", _fake_extract),
        patch.object(entities, "add_relations_batch", _spy_batch),
    ):
        result = _run(entities.extract_entities_batch([5], tenant_id="t1"))

    assert seen, "add_relations_batch was never called"
    rows = seen[0]
    for src, tgt, *_rest in rows:
        assert src is not None and tgt is not None
        assert isinstance(src, int) and isinstance(tgt, int)
        assert src >= 0 and tgt >= 0
    assert len(rows) == 1, f"the junk-endpoint relation must be dropped: {rows!r}"
    assert rows[0][2] == "works_at"
    assert UUID_NAME not in _entity_names_inserted(cur)
    assert result["relations_stored"] == 1


def test_extract_batch_reports_only_stored_entities() -> None:
    """entities_stored counts what was stored, not what the LLM proposed."""
    cur = _id_cursor(fact_rows=[{"id": 5, "fact_text": "Alice works at Acme"}])

    async def _fake_extract(_text: str) -> dict[str, Any]:
        return _extraction_payload()

    async def _noop_batch(rows: list[tuple[Any, ...]], **kwargs: Any) -> int:
        return len(rows)

    with (
        patch.object(entities, "get_connection", lambda **kw: _mock_conn(cur)),
        patch.object(entities, "extract_entities", _fake_extract),
        patch.object(entities, "add_relations_batch", _noop_batch),
    ):
        result = _run(entities.extract_entities_batch([5], tenant_id="t1"))

    assert result["entities_stored"] == 2, "3 extracted, 1 junk -> 2 stored"


def test_extract_and_store_entities_skips_junk() -> None:
    """The single-content path applies the same guard and count."""
    cur = _id_cursor()
    seen: list[list[tuple[Any, ...]]] = []

    async def _fake_extract(_text: str) -> dict[str, Any]:
        return _extraction_payload()

    async def _spy_batch(rows: list[tuple[Any, ...]], **kwargs: Any) -> int:
        seen.append(rows)
        return len(rows)

    with (
        patch.object(entities, "get_connection", lambda **kw: _mock_conn(cur)),
        patch.object(entities, "extract_entities", _fake_extract),
        patch.object(entities, "add_relations_batch", _spy_batch),
    ):
        result = _run(entities.extract_and_store_entities("Alice works at Acme", fact_id=9))

    assert result == {"entities_stored": 2, "relations_stored": 1}
    assert seen[0] == [(101, 102, "works_at", 9, 1.0)]
    assert UUID_NAME not in _entity_names_inserted(cur)
