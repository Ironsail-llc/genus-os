import pytest

pytestmark = pytest.mark.integration

from contextlib import contextmanager

from routers import runs


def _bind(monkeypatch, db_conn):
    @contextmanager
    def _fake():
        yield db_conn

    monkeypatch.setattr(runs, "get_connection", _fake)


def test_list_runs_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    assert isinstance(runs._list_runs(None, 5), list)


def test_steps_and_events_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    assert isinstance(runs._get_steps("00000000-0000-0000-0000-000000000000"), list)
    assert isinstance(runs._get_guardrail_events("00000000-0000-0000-0000-000000000000"), list)
