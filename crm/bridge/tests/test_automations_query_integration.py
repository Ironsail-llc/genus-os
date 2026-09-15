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
    assert isinstance(automations._latest_runs(["nobody"]), dict)


def test_reset_breaker_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    # No such agent: the statement has to be valid to answer False at all.
    assert automations._reset_breaker("no-such-agent", "default") is False
