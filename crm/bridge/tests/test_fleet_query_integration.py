"""Proves the fleet SQL is valid against a real schema (disposable robothor_test),
never production. Only the query helpers touch the DB; the HTTP layer is unit-tested."""

import pytest

pytestmark = pytest.mark.integration

from routers import fleet


def test_schedule_rows_query_runs(db_conn, monkeypatch):
    # _schedule_rows opens its own connection; point get_connection at the test conn.
    from contextlib import contextmanager

    @contextmanager
    def _fake_conn():
        yield db_conn

    monkeypatch.setattr(fleet, "get_connection", _fake_conn)
    rows = fleet._schedule_rows()
    assert isinstance(rows, dict)


def test_run_stats_query_runs(db_conn, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _fake_conn():
        yield db_conn

    monkeypatch.setattr(fleet, "get_connection", _fake_conn)
    stats = fleet._run_stats()
    assert isinstance(stats, dict)
