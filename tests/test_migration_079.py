"""Schema + behavior tests for migration 079_guardrail_event_modes.sql.

Adds the ``observed`` action (observe-mode shadow decisions) and a ``mode``
column to ``agent_guardrail_events``, supporting the observe → alert → enforce
rollout ladder. The migration is idempotent, so the test applies it directly and
asserts the resulting schema + CHECK behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "crm" / "migrations" / "079_guardrail_event_modes.sql"
)


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s", (table,))
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column),
    )
    return cur.fetchone() is not None


@pytest.fixture
def _apply_079(db_cursor, db_conn):
    if not _table_exists(db_cursor, "agent_guardrail_events"):
        pytest.skip("agent_guardrail_events (migration 014) not present in test DB")
    db_cursor.execute(_MIGRATION.read_text())
    yield db_cursor


def _action_check_def(cur) -> str:
    cur.execute(
        """
        SELECT pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'agent_guardrail_events'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%action%'
        """
    )
    row = cur.fetchone()
    return row["def"] if row else ""


class TestMigration079:
    def test_mode_column_added(self, _apply_079):
        assert _column_exists(_apply_079, "agent_guardrail_events", "mode")

    def test_observed_added_to_action_check(self, _apply_079):
        # 'observed' (shadow decisions) must join the allowed actions; the prior
        # values stay allowed.
        check = _action_check_def(_apply_079)
        for action in ("blocked", "warned", "allowed", "observed"):
            assert action in check, f"action CHECK missing '{action}': {check}"

    def test_check_still_constrains(self, _apply_079):
        # The CHECK is still an enumerated allow-list, not dropped entirely.
        check = _action_check_def(_apply_079)
        assert check and "banana_action" not in check

    def test_idempotent_reapply(self, _apply_079):
        # Re-running the migration is a no-op, not an error.
        _apply_079.execute(_MIGRATION.read_text())
        assert _column_exists(_apply_079, "agent_guardrail_events", "mode")
