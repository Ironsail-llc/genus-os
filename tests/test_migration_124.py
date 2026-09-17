"""Schema + behaviour tests for 124_guardrail_context_overflow.sql.

Adds ``context_overflow`` to the ``agent_guardrail_events`` action CHECK, so
the engine's context-shrink control leaves evidence under its own name instead
of being folded into ``warned``. The migration is idempotent, so the test
applies it directly and asserts the resulting constraint.

The sibling of ``test_migration_079.py``, which added ``observed`` the same
way — and deliberately shaped like it, because the next person to extend this
vocabulary should find one pattern rather than two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "crm"
    / "migrations"
    / "124_guardrail_context_overflow.sql"
)


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s", (table,))
    return cur.fetchone() is not None


@pytest.fixture
def _apply_124(db_cursor, db_conn):
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


class TestMigration124:
    def test_context_overflow_joins_the_allowed_actions(self, _apply_124):
        check = _action_check_def(_apply_124)
        assert "context_overflow" in check, check

    def test_every_earlier_action_still_passes(self, _apply_124):
        """079 added `observed` by DROPping and re-ADDing the constraint. A
        migration that forgets one of the older values silently stops every
        guardrail writing at all."""
        check = _action_check_def(_apply_124)
        for action in ("blocked", "warned", "allowed", "observed"):
            assert action in check, f"action CHECK lost '{action}': {check}"

    def test_the_check_is_still_an_allow_list(self, _apply_124):
        check = _action_check_def(_apply_124)
        assert check and "banana_action" not in check

    def test_a_context_overflow_row_can_actually_be_written(self, _apply_124):
        """The constraint definition is a string; this is the behaviour.

        Attached to an existing run because ``run_id`` is a foreign key. The
        fixture connection rolls back in teardown, so neither this row nor the
        migration above it survives the test.
        """
        _apply_124.execute("SELECT id FROM agent_runs LIMIT 1")
        run = _apply_124.fetchone()
        if run is None:
            pytest.skip("no agent_runs row to attach a guardrail event to")

        _apply_124.execute(
            """
            INSERT INTO agent_guardrail_events (
                run_id, step_number, guardrail_name, action, reason, mode
            )
            VALUES (%s, 0, 'context_overflow', 'context_overflow', 'probe', 'enforce')
            RETURNING action
            """,
            (run["id"],),
        )
        assert _apply_124.fetchone()["action"] == "context_overflow"

    def test_idempotent_reapply(self, _apply_124):
        _apply_124.execute(_MIGRATION.read_text())
        assert "context_overflow" in _action_check_def(_apply_124)
