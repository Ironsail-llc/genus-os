"""Chat-turn TTL sweep — real DB, no mocks.

``cleanup_stale_chat_turns`` is the nightly GC that keeps ``chat_messages``
from growing without bound. Its tenant-scoped branch filtered on
``chat_messages.tenant_id``, a column that does not exist: tenancy lives on
``chat_sessions``, and ``chat_messages`` reaches it only through
``session_id``. Its sole caller (``robothor/memory/lifecycle.py``, step 10)
always passes a tenant, so every run raised ``UndefinedColumn`` and the
maintenance pass swallowed it as a warning — the TTL had never pruned a row.

A mocked cursor cannot catch this. The bug is that the SQL does not match the
schema, so only a real Postgres parse proves the fix. The tests below pin:

1. The sweep runs and returns a row count instead of raising.
2. It deletes only the requested tenant's stale turns.
3. Another tenant's stale turns, and the same tenant's recent/pinned turns,
   are left alone.
"""

from __future__ import annotations

import json

import pytest

from robothor.engine.chat_store import cleanup_stale_chat_turns

pytestmark = pytest.mark.integration


def _seed_session(cur, *, tenant: str, session_key: str) -> int:
    """Create one chat session for ``tenant`` and return its id."""
    cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) "
        "VALUES (%s, %s, 'telegram') RETURNING id",
        (tenant, session_key),
    )
    return int(cur.fetchone()["id"])


def _seed_turn(
    cur,
    *,
    session_id: int,
    content: str,
    age_days: int,
    pinned: bool = False,
) -> int:
    """Insert one chat turn aged ``age_days`` into the past."""
    cur.execute(
        "INSERT INTO chat_messages (session_id, message, created_at, pinned) "
        "VALUES (%s, %s::jsonb, NOW() - make_interval(days => %s), %s) RETURNING id",
        (
            session_id,
            json.dumps({"role": "user", "content": content}),
            age_days,
            pinned,
        ),
    )
    return int(cur.fetchone()["id"])


@pytest.fixture
def two_tenant_chat(db_cursor, test_prefix):
    """Two tenants, each with a session holding stale + fresh + pinned turns."""
    tenant_a = f"{test_prefix}-a"
    tenant_b = f"{test_prefix}-b"
    for tenant in (tenant_a, tenant_b):
        db_cursor.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tenant, tenant),
        )

    session_a = _seed_session(db_cursor, tenant=tenant_a, session_key=f"{test_prefix}:a")
    session_b = _seed_session(db_cursor, tenant=tenant_b, session_key=f"{test_prefix}:b")

    ids = {
        "a_stale_1": _seed_turn(db_cursor, session_id=session_a, content="a old one", age_days=200),
        "a_stale_2": _seed_turn(db_cursor, session_id=session_a, content="a old two", age_days=120),
        "a_fresh": _seed_turn(db_cursor, session_id=session_a, content="a recent", age_days=3),
        "a_pinned": _seed_turn(
            db_cursor, session_id=session_a, content="a keep me", age_days=300, pinned=True
        ),
        "b_stale": _seed_turn(db_cursor, session_id=session_b, content="b old one", age_days=200),
    }
    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "session_a": session_a,
        "session_b": session_b,
        "ids": ids,
    }


def _surviving(cur, ids: dict[str, int]) -> set[str]:
    """Names of the seeded turns still present in the table."""
    cur.execute(
        "SELECT id FROM chat_messages WHERE id = ANY(%s)",
        (list(ids.values()),),
    )
    alive = {int(r["id"]) for r in cur.fetchall()}
    return {name for name, mid in ids.items() if mid in alive}


def test_tenant_sweep_returns_count_without_raising(
    two_tenant_chat, mock_get_connection, db_cursor
):
    """The tenant-scoped branch must execute — it referenced a phantom column.

    Before the fix this raised psycopg2.errors.UndefinedColumn: column
    "tenant_id" of relation "chat_messages" does not exist.
    """
    deleted = cleanup_stale_chat_turns(90, two_tenant_chat["tenant_a"])

    assert isinstance(deleted, int)
    assert deleted == 2, "both of tenant A's stale, unpinned turns should be pruned"


def test_tenant_sweep_deletes_only_that_tenants_stale_turns(
    two_tenant_chat, mock_get_connection, db_cursor
):
    """Scoping is the whole point: one tenant's GC must not touch another's."""
    cleanup_stale_chat_turns(90, two_tenant_chat["tenant_a"])

    survivors = _surviving(db_cursor, two_tenant_chat["ids"])
    assert survivors == {"a_fresh", "a_pinned", "b_stale"}, (
        "expected tenant A's stale turns gone and everything else untouched"
    )


def test_global_sweep_still_prunes_across_tenants(two_tenant_chat, mock_get_connection, db_cursor):
    """The ``tenant_id=None`` branch (manual one-off GC) stays unscoped."""
    cleanup_stale_chat_turns(90, None)

    survivors = _surviving(db_cursor, two_tenant_chat["ids"])
    assert survivors == {"a_fresh", "a_pinned"}, "global sweep should prune every tenant"
