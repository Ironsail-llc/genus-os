"""Every remote-tools audit row reaches the real ``audit_log``.

The unit tests fake the event writer, so a value too long for a column
passed them while Postgres dropped the row. This writes every outcome the
target audits, and every home outcome, through the real ``log_event`` and
reads each row back.
"""

from __future__ import annotations

from typing import Any

import pytest

MIGRATION = "184_remote_tool_requests"


@pytest.fixture
def real_audit(scratch_db, monkeypatch):
    import psycopg2

    from robothor.audit import logger as audit_logger

    db, dsn = scratch_db(through=MIGRATION)
    audit_logger.set_connection_factory(lambda: psycopg2.connect(dsn))
    monkeypatch.setattr(audit_logger, "_release_connection", lambda conn: conn.close())

    class _Now:
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> None:
            fn(*args, **kwargs)

    monkeypatch.setattr("robothor.engine.event_writer.get_event_writer", lambda: _Now())
    yield db
    audit_logger.reset_connection_factory()


@pytest.mark.integration
def test_the_column_limits_are_the_tables(real_audit) -> None:
    from robothor.audit.logger import COLUMN_LIMITS

    with real_audit.cursor() as cur:
        cur.execute(
            "SELECT column_name, character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'audit_log' AND character_maximum_length IS NOT NULL"
        )
        real = dict(cur.fetchall())
    for column, limit in COLUMN_LIMITS.items():
        assert real[column] == limit, column


@pytest.mark.integration
def test_every_target_outcome_is_written(real_audit) -> None:
    from robothor.engine.remote_tools import audit
    from robothor.tenancy import remote_contract as rc

    outcomes = sorted(rc.ERROR_CODES | rc.OUTCOMES | {"internal_error"})
    for outcome in outcomes:
        audit(
            tenant_id="test-tenant",
            home_tenant="h" * 128,
            user_id="",
            agent_id="a" * 64,
            tool="example_write",
            access="write",
            outcome=outcome,
            dry_run=False,
            request_id="req-1",
            args={"id": 1},
        )

    with real_audit.cursor() as cur:
        cur.execute(
            "SELECT details->>'outcome', status FROM audit_log WHERE event_type = %s",
            (rc.EVENT_REMOTE_TOOL_CALL,),
        )
        rows = dict(cur.fetchall())
    assert sorted(rows) == outcomes
    assert set(rows.values()) <= rc.AUDIT_STATUSES


@pytest.mark.integration
def test_every_home_outcome_is_written(real_audit) -> None:
    from robothor.engine.tools.cross_tenant import CROSS_TENANT_EVENT, audit_cross_tenant

    outcomes = ["ok", "error", "denied", "replay", "unknown"]
    for outcome in outcomes:
        audit_cross_tenant(
            home_tenant="test-tenant",
            target_tenant="t" * 128,
            user_id="",
            agent_id="a" * 64,
            tool="org_tool_write",
            outcome=outcome,
            reason="r" * 5000,
            remote_tool="example_write",
        )

    with real_audit.cursor() as cur:
        cur.execute("SELECT status FROM audit_log WHERE event_type = %s", (CROSS_TENANT_EVENT,))
        assert sorted(r[0] for r in cur.fetchall()) == sorted(outcomes)


@pytest.mark.integration
def test_a_held_crossing_is_written(real_audit) -> None:
    from robothor.engine.tools.cross_tenant import CROSS_TENANT_EVENT, audit_cross_tenant
    from robothor.tenancy.remote_contract import OUTCOME_HELD_UNCERTAIN

    audit_cross_tenant(
        home_tenant="test-tenant",
        target_tenant="t" * 128,
        user_id="",
        agent_id="a" * 64,
        tool="org_tool_write",
        outcome="denied",
        remote_tool="example_write",
        code=OUTCOME_HELD_UNCERTAIN,
        held={"effect_id": "e-1", "digest": "d" * 64},
    )

    with real_audit.cursor() as cur:
        cur.execute(
            "SELECT status, details->>'outcome', details->'held'->>'digest' FROM audit_log "
            "WHERE event_type = %s",
            (CROSS_TENANT_EVENT,),
        )
        assert cur.fetchall() == [("denied", OUTCOME_HELD_UNCERTAIN, "d" * 64)]
