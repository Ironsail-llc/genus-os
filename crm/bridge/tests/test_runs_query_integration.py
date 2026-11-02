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
    # Keyword-only since the list gained filters.
    assert isinstance(runs._list_runs(limit=5), list)


def test_filtered_list_summary_and_detail_sql_valid(db_conn, monkeypatch):
    """The filtered list, the summary's delivery CASE and the detail columns
    all run against the real schema, not only a fake ``_rows``."""
    _bind(monkeypatch, db_conn)
    rows = runs._list_runs(
        status="failed", agent="example-agent", trigger="cron", since=24, limit=5, offset=0
    )
    assert isinstance(rows, list)
    summary = runs._summary(since=24)
    assert set(summary["by_delivery"]) >= {"delivered", "none_expected", "failed", "pending"}
    assert runs._get_run("00000000-0000-0000-0000-000000000000") is None


def test_steps_and_events_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    assert isinstance(runs._get_steps("00000000-0000-0000-0000-000000000000"), list)
    assert isinstance(runs._get_guardrail_events("00000000-0000-0000-0000-000000000000"), list)
