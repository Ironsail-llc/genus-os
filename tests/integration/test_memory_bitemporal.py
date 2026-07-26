"""Bi-temporal validity: an update bounds the past, it does not erase it.

conflicts.py treats "contradiction" and "update" as the same event and answers
both by deactivating the old row. They are not the same. An update means the
world changed and the old fact was true until it wasn't; a contradiction means
one of the two claims is wrong. Collapsing them means the system cannot answer
"what did we decide last month" — last month's truth was deleted rather than
bounded, which is exactly the long-horizon failure this overhaul targets.

These tests pin: the two classifications are recorded distinctly, an update
bounds the old fact with valid_to instead of only hiding it, and a point-in-time
query can still see what was true then.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.memory.bitemporal import (
    bitemporal_enabled,
    point_in_time_predicate,
    record_conflict_decision,
    supersede_with_validity,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT = "bitemporal-test"


@pytest.fixture
def clean_tenant():
    """Isolated tenant, wiped before and after. Local rather than shared: these
    tests deactivate and bound facts, which no other suite should ever see."""
    from robothor.db.connection import get_connection

    def _wipe():
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM memory_conflict_decisions WHERE tenant_id = %s", (TENANT,))
            cur.execute("DELETE FROM memory_facts WHERE tenant_id = %s", (TENANT,))
            conn.commit()

    with get_connection() as conn:
        # memory_facts.tenant_id is a real FK; a bare string is not a tenant.
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (TENANT, "Bi-temporal test tenant"),
        )
        conn.commit()
    _wipe()
    yield TENANT
    _wipe()


@pytest.fixture
def seed_fact():
    from robothor.db.connection import get_connection

    def _seed(text: str, category: str = "decision") -> int:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO memory_facts (fact_text, category, tenant_id, is_active) "
                "VALUES (%s, %s, %s, TRUE) RETURNING id",
                (text, category, TENANT),
            )
            fid = cur.fetchone()[0]
            conn.commit()
        return fid

    return _seed


class TestPointInTimePredicate:
    """Pure. The NULL semantics here decide whether 152k existing facts stay
    visible when the flag flips, so they get their own tests."""

    def test_null_valid_from_is_unbounded_past(self):
        # Every pre-existing fact has valid_from NULL. If NULL meant "not yet
        # valid", flipping the flag would blank the entire backlog.
        sql, params = point_in_time_predicate(datetime(2026, 1, 1, tzinfo=UTC))
        assert "valid_from IS NULL" in sql

    def test_null_valid_to_is_still_true(self):
        sql, _ = point_in_time_predicate(datetime(2026, 1, 1, tzinfo=UTC))
        assert "valid_to IS NULL" in sql

    def test_none_asof_produces_no_filter(self):
        sql, params = point_in_time_predicate(None)
        assert sql == ""
        assert params == []


class TestConflictDecisionsAreRecorded:
    async def test_decision_row_is_written(self, clean_tenant):
        did = record_conflict_decision(
            tenant_id=TENANT,
            classification="update",
            action="superseded",
            new_fact_id=None,
            existing_fact_id=None,
            reasoning="the meeting moved",
            similarity=0.81,
            new_fact_text="Standup moved to 10am",
            existing_fact_text="Standup is at 9am",
        )
        assert did is not None

        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT classification, action, reasoning, review_verdict "
                "FROM memory_conflict_decisions WHERE id = %s",
                (did,),
            )
            row = cur.fetchone()
        assert row[0] == "update"
        assert row[1] == "superseded"
        assert row[2] == "the meeting moved"
        # Unreviewed is not the same as correct.
        assert row[3] is None

    async def test_contradiction_and_update_are_distinguishable(self, clean_tenant):
        record_conflict_decision(
            tenant_id=TENANT, classification="update", action="superseded",
            new_fact_id=None, existing_fact_id=None,
        )
        record_conflict_decision(
            tenant_id=TENANT, classification="contradiction", action="superseded",
            new_fact_id=None, existing_fact_id=None,
        )
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT classification, count(*) FROM memory_conflict_decisions "
                "WHERE tenant_id = %s GROUP BY 1 ORDER BY 1",
                (TENANT,),
            )
            got = dict(cur.fetchall())
        # The whole point: after the fact you can ask how often each happened.
        assert got == {"contradiction": 1, "update": 1}


class TestSupersedeSetsValidity:
    async def test_update_bounds_the_old_fact(self, clean_tenant, seed_fact):
        old_id = seed_fact("Standup is at 9am")
        new_id = seed_fact("Standup moved to 10am")

        supersede_with_validity(old_id, new_id, tenant_id=TENANT, classification="update")

        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT is_active, superseded_by, valid_to FROM memory_facts WHERE id = %s",
                (old_id,),
            )
            active, superseded_by, valid_to = cur.fetchone()
            cur.execute("SELECT valid_from FROM memory_facts WHERE id = %s", (new_id,))
            (new_valid_from,) = cur.fetchone()

        # Existing behaviour preserved — no retrieval regression.
        assert active is False
        assert superseded_by == new_id
        # New behaviour: the past is bounded, not erased.
        assert valid_to is not None
        assert new_valid_from is not None

    async def test_point_in_time_still_sees_the_bounded_fact(self, clean_tenant, seed_fact):
        old_id = seed_fact("Standup is at 9am")
        new_id = seed_fact("Standup moved to 10am")
        before = datetime.now(UTC) - timedelta(hours=1)
        supersede_with_validity(old_id, new_id, tenant_id=TENANT, classification="update")

        sql, params = point_in_time_predicate(before)
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT id FROM memory_facts WHERE tenant_id = %s AND id = %s AND {sql}",
                [TENANT, old_id, *params],
            )
            rows = cur.fetchall()

        # "What was true an hour ago" must still find the 9am standup, even
        # though it is inactive now. This is the capability the single time
        # axis made impossible.
        assert [r[0] for r in rows] == [old_id]

    async def test_point_in_time_excludes_it_after_the_bound(self, clean_tenant, seed_fact):
        old_id = seed_fact("Standup is at 9am")
        new_id = seed_fact("Standup moved to 10am")
        supersede_with_validity(old_id, new_id, tenant_id=TENANT, classification="update")

        sql, params = point_in_time_predicate(datetime.now(UTC) + timedelta(hours=1))
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT id FROM memory_facts WHERE tenant_id = %s AND id = %s AND {sql}",
                [TENANT, old_id, *params],
            )
            assert cur.fetchall() == []


class TestFlagIsOffByDefault:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("MEMORY_BITEMPORAL", raising=False)
        assert bitemporal_enabled() is False

    def test_on_when_set(self, monkeypatch):
        monkeypatch.setenv("MEMORY_BITEMPORAL", "1")
        assert bitemporal_enabled() is True
