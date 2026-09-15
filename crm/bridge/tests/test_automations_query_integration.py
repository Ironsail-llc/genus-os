"""The automations statements, executed against a real schema.

Unit tests here mock ``_query`` and prove the composition; nothing in them
would catch a column that does not exist. These two do, and only these two.
"""

import pytest

pytestmark = pytest.mark.integration

from contextlib import contextmanager

from routers import automations


def _bind(monkeypatch, db_conn):
    @contextmanager
    def _fake():
        yield db_conn

    monkeypatch.setattr(automations, "get_connection", _fake)


def test_schedule_and_latest_run_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    assert isinstance(automations._schedule_rows("default"), list)
    assert isinstance(automations._latest_runs(["nobody"], "default"), dict)


def test_reset_breaker_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    # No such agent: the statement has to be valid to answer False at all.
    assert automations._reset_breaker("no-such-agent", "default") is False
    # And the job-id shape the engine actually writes.
    assert automations._reset_breaker("no-such-agent:heartbeat", "default") is False


def test_a_foreign_tenants_newer_run_is_not_the_card_s_last_run(db_conn, monkeypatch):
    """The predicate that C2 added, proved against a real table.

    Two runs for the same agent id, the newer one belonging to another tenant.
    Without ``AND tenant_id = %s`` the ``DISTINCT ON`` hands back the foreign
    row, and its delivery status and verification verdict land on this
    automation's card.
    """
    _bind(monkeypatch, db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            ("probe-other", "Probe Other"),
        )
        for tenant, status, minutes in (("default", "completed", 60), ("probe-other", "failed", 1)):
            cur.execute(
                "INSERT INTO agent_runs (tenant_id, agent_id, trigger_type, status, started_at) "
                "VALUES (%s, %s, 'cron', %s, NOW() - make_interval(mins => %s))",
                (tenant, "probe-two-tenants", status, minutes),
            )

    latest = automations._latest_runs(["probe-two-tenants"], "default")
    assert latest["probe-two-tenants"]["status"] == "completed"
    db_conn.rollback()
