"""169: the shared-threads schema, its row-level security and its role seed.

Applied to a scratch database at the point in the chain where it lands, then
applied again: every statement must be idempotent.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATION = "169_rooms"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
ROOM_TABLES = ("rooms", "room_members", "room_revisions", "room_editions", "room_tasks")


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def _columns(cur: Any, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    )
    return {r[0] for r in cur.fetchall()}


@pytest.mark.integration
def test_it_applies_twice_cleanly(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        cur.execute(SQL_FILE.read_text())
        for table in ROOM_TABLES:
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass",
                (table,),
            )
            assert cur.fetchone() == (True, True), table


@pytest.mark.integration
def test_the_tables_carry_the_contracts_columns(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        assert {
            "tenant_id", "id", "kind", "title", "agent_id", "session_key", "created_by",
            "run_as_user_id", "instructions", "settings", "task_id", "paused",
            "consecutive_failures", "rev", "last_activity_at", "last_message_id",
            "archived_at", "created_at", "updated_at",
        } <= _columns(cur, "rooms")  # fmt: skip
        assert {
            "tenant_id", "room_id", "user_id", "assigned", "added_by", "last_read_message_id",
            "last_read_published_seq", "notify", "created_at", "updated_at",
        } <= _columns(cur, "room_members")  # fmt: skip
        assert {
            "tenant_id", "room_id", "rev", "changed_by", "changed_at", "definition",
        } <= _columns(cur, "room_revisions")  # fmt: skip
        assert {
            "tenant_id", "id", "room_id", "seq", "kind", "status", "preview", "period_start",
            "period_end", "grain", "revision", "published_seq", "title", "summary", "blocks",
            "suggestions",
            "provenance", "error", "run_id", "chat_message_id", "requested_by",
            "created_at", "updated_at",
        } <= _columns(cur, "room_editions")  # fmt: skip
        assert {
            "tenant_id", "room_id", "task_id", "edition_id", "origin", "suggestion_key",
            "accepted_by", "dismissed_by", "created_at", "updated_at",
        } <= _columns(cur, "room_tasks")  # fmt: skip
        assert {"room_id", "run_as_user_id"} <= _columns(cur, "user_cronjobs")
        assert {"proposed_by_run_id", "proposed_for_user_id", "room_id"} <= _columns(
            cur, "operation_actions"
        )


@pytest.mark.integration
def test_one_room_per_task_per_tenant(scratch_db) -> None:
    import uuid

    import psycopg2

    db, _ = scratch_db(through=MIGRATION)
    task = str(uuid.uuid4())

    def insert(tenant: str) -> None:
        room = str(uuid.uuid4())
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, "
                "created_by, task_id) VALUES (%s, %s, 'task', 't', 'main', %s, %s, %s)",
                (tenant, room, f"agent:main:room:{room}", str(uuid.uuid4()), task),
            )

    insert("test-tenant")
    insert("test-tenant-other")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        insert("test-tenant")


def _decision(dsn: str, monkeypatch, role: str, tool: str) -> str | None:
    import psycopg2

    from robothor.engine import permissions

    @contextlib.contextmanager
    def connect(**_kw: Any) -> Any:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    permissions.invalidate_permission_cache()
    return permissions.check_tool_permission(role, "test-tenant", tool)


ROOM_TOOLS = ("room_create", "room_list", "room_read", "room_run", "room_task_accept")
ADMIN_TOOLS = ("room_admin_reassign",)


@pytest.mark.integration
def test_the_role_seed(scratch_db, monkeypatch) -> None:
    _, dsn = scratch_db(through=MIGRATION)
    decisions = {
        (role, tool): _decision(dsn, monkeypatch, role, tool) is None
        for role in ("owner", "admin", "member", "user", "viewer")
        for tool in (*ROOM_TOOLS, *ADMIN_TOOLS)
    }
    for role in ("owner", "admin"):
        assert all(decisions[(role, t)] for t in (*ROOM_TOOLS, *ADMIN_TOOLS)), role
    for role in ("member", "user"):
        assert all(decisions[(role, t)] for t in ROOM_TOOLS), role
        assert not any(decisions[(role, t)] for t in ADMIN_TOOLS), role
    assert {t for t in (*ROOM_TOOLS, *ADMIN_TOOLS) if decisions[("viewer", t)]} == {
        "room_list",
        "room_read",
    }
