"""175: a workspace's history page is read by its session's own index.

The page query (``chat_store.load_history_page``) filters one session and
orders by message id, newest first. With only ``(session_id, created_at)``
indexed, the planner walked the whole table's primary key backwards and threw
away every other session's rows: a quiet workspace's newest page cost as much
as every message written anywhere since it was last active. Measured before
this migration: 300,051 rows read to return 51.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATION = "175_chat_messages_session_id"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def _session(cur: Any, key: str) -> int:
    cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) "
        "VALUES ('test-tenant', %s, 'webchat') RETURNING id",
        (key,),
    )
    return int(cur.fetchone()[0])


@pytest.mark.integration
def test_it_applies_twice_and_indexes_session_then_id(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        cur.execute(SQL_FILE.read_text())
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'chat_messages' "
            "AND indexname = 'idx_chat_messages_session_id'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "(session_id, id)" in row[0]


@pytest.mark.integration
def test_a_quiet_sessions_page_does_not_read_a_busy_ones_rows(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        quiet = _session(cur, "agent:main:room:quiet")
        # Production has many sessions; the planner's estimate depends on it.
        cur.execute(
            "INSERT INTO chat_sessions (tenant_id, session_key, channel) "
            "SELECT 'test-tenant', 'agent:main:room:busy-' || g, 'webchat' "
            "FROM generate_series(1, 400) g"
        )
        cur.execute(
            "INSERT INTO chat_messages (session_id, message) "
            "SELECT %s, jsonb_build_object('role', 'user', 'content', 'q' || g) "
            "FROM generate_series(1, 60) g",
            (quiet,),
        )
        # Everyone else talks after it: 20,000 newer messages over 400 sessions.
        cur.execute(
            "INSERT INTO chat_messages (session_id, message) "
            "SELECT s.id, jsonb_build_object('role', 'user', 'content', 'b' || g) "
            "FROM generate_series(1, 50) g CROSS JOIN chat_sessions s "
            "WHERE s.session_key LIKE 'agent:main:room:busy-%%'"
        )
        cur.execute("ANALYZE chat_messages")
        cur.execute("ANALYZE chat_sessions")
        plans: list[dict[str, Any]] = []
        from robothor.engine.chat_store import HISTORY_PAGE_SQL

        # The query ``load_history_page`` runs: the newest page, then one back.
        for before in (None, 2**62):
            cur.execute(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + HISTORY_PAGE_SQL,
                ("test-tenant", "agent:main:room:quiet", before, before, 51, 0),
            )
            plans.append(cur.fetchone()[0][0]["Plan"])

    def nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
        return [node] + [n for child in node.get("Plans", []) for n in nodes(child)]

    for plan in plans:
        scans = [n for n in nodes(plan) if n.get("Relation Name") == "chat_messages"]
        assert scans, plan
        assert all(n.get("Index Name") != "chat_messages_pkey" for n in scans), scans
        assert sum(int(n.get("Actual Rows", 0)) for n in scans) <= 60, scans
