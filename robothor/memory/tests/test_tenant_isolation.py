"""Tests for cross-tenant memory isolation.

Verifies that tenant_id filtering prevents data leaking between tenants
in both facts and entities modules.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


def _run(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Facts: search_facts isolates tenants
# ---------------------------------------------------------------------------


@patch("robothor.memory.facts.get_connection")
@patch("robothor.memory.facts.llm_client")
def test_search_facts_isolates_tenants(mock_llm, mock_get_conn):
    """Querying facts for tenant_a must not return tenant_b data.

    We mock the DB cursor to inspect the SQL params and verify the
    tenant_id is passed through to the WHERE clause.
    """
    from robothor.memory.facts import search_facts

    mock_embedding = [0.1] * 384

    async def _fake_embed(*a, **kw):
        return mock_embedding

    mock_llm.get_embedding_async = _fake_embed

    mock_cur = MagicMock()
    # Vector search returns one result for tenant_a
    tenant_a_row = {
        "id": 1,
        "fact_text": "Alice works at Acme",
        "category": "work",
        "entities": ["Alice"],
        "confidence": 0.9,
        "importance_score": 0.7,
        "access_count": 2,
        "embedding": mock_embedding,
    }
    mock_cur.fetchall.return_value = [tenant_a_row]
    mock_cur.description = [
        ("id",),
        ("fact_text",),
        ("category",),
        ("entities",),
        ("confidence",),
        ("importance_score",),
        ("access_count",),
        ("embedding",),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    # Search as tenant_a
    _run(search_facts("Alice", tenant_id="tenant_a"))

    # Verify tenant_id was passed to the SQL query
    calls = mock_cur.execute.call_args_list
    assert len(calls) > 0
    # The vector search query should contain tenant_a
    sql_params = calls[0][0][1]  # second positional arg = params tuple
    assert "tenant_a" in sql_params, f"tenant_a not found in SQL params: {sql_params}"


# ---------------------------------------------------------------------------
# Entities: get_entity isolates tenants
# ---------------------------------------------------------------------------


@patch("robothor.memory.entities.get_connection")
def test_get_entity_isolates_tenants(mock_get_conn):
    """get_entity for tenant_a must filter by tenant_id in the WHERE clause."""
    from robothor.memory.entities import get_entity

    mock_cur = MagicMock()
    # First call: entity lookup returns None for tenant_b
    mock_cur.fetchone.return_value = None
    mock_cur.fetchall.return_value = []

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    result = _run(get_entity("Alice", tenant_id="tenant_b"))
    assert result is None

    # Verify SQL includes tenant_id
    execute_call = mock_cur.execute.call_args
    sql = execute_call[0][0]
    params = execute_call[0][1]
    assert "tenant_id" in sql, f"tenant_id not in SQL: {sql}"
    assert "tenant_b" in params, f"tenant_b not in params: {params}"


@patch("robothor.memory.entities.get_connection")
def test_get_entity_returns_entity_for_matching_tenant(mock_get_conn):
    """get_entity returns data when queried with the correct tenant."""
    from robothor.memory.entities import get_entity

    mock_cur = MagicMock()
    entity_row = {
        "id": 42,
        "name": "Bob",
        "entity_type": "person",
        "aliases": [],
        "mention_count": 5,
        "tenant_id": "tenant_a",
    }
    # First call returns the entity, subsequent calls return empty relations
    mock_cur.fetchone.return_value = entity_row
    mock_cur.fetchall.return_value = []

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    result = _run(get_entity("Bob", tenant_id="tenant_a"))
    assert result is not None
    assert result["name"] == "Bob"

    # All three SQL calls (entity + outgoing + incoming) should filter by tenant_a
    for call in mock_cur.execute.call_args_list:
        sql = call[0][0]
        params = call[0][1]
        assert "tenant_id" in sql, f"tenant_id missing from SQL: {sql}"
        assert "tenant_a" in params, f"tenant_a missing from params: {params}"


# ---------------------------------------------------------------------------
# Entities: upsert_entity includes tenant_id
# ---------------------------------------------------------------------------


@patch("robothor.memory.entities.get_connection")
def test_upsert_entity_includes_tenant_id(mock_get_conn):
    """upsert_entity must include tenant_id in the INSERT statement."""
    from robothor.memory.entities import upsert_entity

    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (99,)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    entity_id = _run(upsert_entity("Alice", "person", tenant_id="tenant_x"))
    assert entity_id == 99

    execute_call = mock_cur.execute.call_args
    sql = execute_call[0][0]
    params = execute_call[0][1]
    assert "tenant_id" in sql, f"tenant_id not in INSERT SQL: {sql}"
    assert "tenant_x" in params, f"tenant_x not in params: {params}"


@patch("robothor.memory.entities.get_connection")
def test_upsert_entity_uses_default_tenant_when_empty(mock_get_conn):
    """upsert_entity falls back to DEFAULT_TENANT when tenant_id is empty."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.entities import upsert_entity

    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (1,)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    _run(upsert_entity("Bob", "person"))

    params = mock_cur.execute.call_args[0][1]
    assert DEFAULT_TENANT in params, f"DEFAULT_TENANT not in params: {params}"


# ---------------------------------------------------------------------------
# Entities: add_relation includes tenant_id
# ---------------------------------------------------------------------------


@patch("robothor.memory.entities.get_connection")
def test_add_relation_includes_tenant_id(mock_get_conn):
    """add_relation must include tenant_id in the INSERT statement."""
    from robothor.memory.entities import add_relation

    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (7,)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    rel_id = _run(add_relation(1, 2, "works_with", tenant_id="tenant_y"))
    assert rel_id == 7

    execute_call = mock_cur.execute.call_args
    sql = execute_call[0][0]
    params = execute_call[0][1]
    assert "tenant_id" in sql, f"tenant_id not in INSERT SQL: {sql}"
    assert "tenant_y" in params, f"tenant_y not in params: {params}"


# ---------------------------------------------------------------------------
# Entities: get_all_about passes tenant through
# ---------------------------------------------------------------------------


@patch("robothor.memory.entities.get_entity")
@patch("robothor.memory.entities.get_connection")
def test_get_all_about_filters_facts_by_tenant(mock_get_conn, mock_get_entity):
    """get_all_about must filter memory_facts by tenant_id."""
    from robothor.memory.entities import get_all_about

    async def _fake_get_entity(*a, **kw):
        return {"id": 1, "name": "Alice", "relations": []}

    mock_get_entity.side_effect = _fake_get_entity

    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = []

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_get_conn.return_value = mock_conn

    _run(get_all_about("Alice", tenant_id="tenant_z"))

    # Verify get_entity was called with tenant_id
    mock_get_entity.assert_called_once_with("Alice", tenant_id="tenant_z")

    # Verify facts query includes tenant_id
    sql = mock_cur.execute.call_args[0][0]
    params = mock_cur.execute.call_args[0][1]
    assert "tenant_id" in sql, f"tenant_id missing from facts SQL: {sql}"
    assert "tenant_z" in params, f"tenant_z missing from facts params: {params}"
