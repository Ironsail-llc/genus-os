"""External n8n monitoring: is the operator's n8n healthy, and which workflows fail.

Read-only and operator-scoped, like Workflows and Health. Honesty rules:
a monitor that is switched off says ``not_configured``; one that has never
polled, or stopped polling, says ``unknown`` rather than a false green; a
database error degrades to ``unknown`` with the error named, never a 500.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from routers import n8n

BASE = "https://n8n.example.com"
POLL = 300


def _now() -> datetime:
    return datetime.now(UTC)


class FakeCursor:
    """Answers by table name, and records every (sql, params) it was given."""

    def __init__(self, tables: dict[str, list[dict]], calls: list):
        self._tables = tables
        self._calls = calls
        self._result: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._calls.append((sql, params))
        for table, rows in self._tables.items():
            if table in sql:
                self._result = rows
                return
        self._result = []

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class FakeConn:
    def __init__(self, tables, calls):
        self._tables = tables
        self._calls = calls

    def cursor(self, *args, **kwargs):
        return FakeCursor(self._tables, self._calls)


@pytest.fixture
def calls():
    return []


@pytest.fixture
def fake_db(monkeypatch, calls):
    """Install a fake connection; returns a setter for the tables' contents."""
    tables: dict[str, list[dict]] = {}

    @contextmanager
    def fake_get_connection(*args, **kwargs):
        yield FakeConn(tables, calls)

    monkeypatch.setattr(n8n, "get_connection", fake_get_connection)

    def set_tables(**contents):
        tables.clear()
        tables.update(contents)

    return set_tables


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(n8n, "_n8n_settings", lambda: (True, BASE, POLL))


def _workflow(**over):
    row = {
        "workflow_id": "wf1",
        "name": "Invoice sync",
        "active": True,
        "tags": ["billing"],
        "failures_24h": 0,
        "failures_7d": 0,
        "last_failure_at": None,
        "last_error": None,
    }
    row.update(over)
    return row


def _sync(**over):
    row = {
        "last_poll_at": _now() - timedelta(seconds=30),
        "last_ok_at": _now() - timedelta(seconds=30),
        "last_error": None,
    }
    row.update(over)
    return row


# --- the gate ---------------------------------------------------------------


def test_viewer_is_refused(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/integrations/n8n/workflows").status_code == 403


def test_service_token_is_refused(controls_client_as_service):
    r = controls_client_as_service.get("/api/integrations/n8n/workflows")
    assert r.status_code == 403


def test_viewer_is_refused_executions(controls_client_as_viewer):
    r = controls_client_as_viewer.get("/api/integrations/n8n/workflows/wf1/executions")
    assert r.status_code == 403


def test_router_is_read_only():
    methods = {m for route in n8n.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}


# --- the listing --------------------------------------------------------------


def test_operator_gets_the_listing(controls_client_as_operator, enabled, fake_db, calls):
    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow()])
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["base_url"] == BASE
    assert body["last_error"] is None
    assert isinstance(body["last_poll_at"], str)
    assert isinstance(body["last_ok_at"], str)
    [wf] = body["workflows"]
    assert wf["workflow_id"] == "wf1"
    assert wf["name"] == "Invoice sync"
    assert wf["active"] is True
    assert wf["tags"] == ["billing"]
    assert wf["failures_24h"] == 0
    assert wf["failures_7d"] == 0
    assert wf["url"] == f"{BASE}/workflow/wf1"


def test_every_query_is_tenant_scoped(controls_client_as_operator, enabled, fake_db, calls):
    from routers._operator import PLATFORM_TENANT

    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow()])
    controls_client_as_operator.get("/api/integrations/n8n/workflows")
    assert calls, "the route must read the tables"
    for sql, params in calls:
        assert "tenant_id" in sql
        assert PLATFORM_TENANT in params


def test_failure_windows_use_started_at(controls_client_as_operator, enabled, fake_db, calls):
    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow()])
    controls_client_as_operator.get("/api/integrations/n8n/workflows")
    listing_sql = " ".join(sql for sql, _ in calls if "n8n_workflows" in sql)
    assert "n8n_executions" in listing_sql
    assert "started_at" in listing_sql


def test_disabled_is_not_configured(controls_client_as_operator, monkeypatch, fake_db):
    monkeypatch.setattr(n8n, "_n8n_settings", lambda: (False, "", POLL))
    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow(failures_24h=3)])
    body = controls_client_as_operator.get("/api/integrations/n8n/workflows").json()
    assert body["status"] == "not_configured"
    assert body["workflows"] == []


def test_a_database_error_is_unknown_not_500(controls_client_as_operator, enabled, monkeypatch):
    @contextmanager
    def boom(*args, **kwargs):
        raise RuntimeError("relation n8n_workflows does not exist")
        yield  # pragma: no cover

    monkeypatch.setattr(n8n, "get_connection", boom)
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unknown"
    assert "does not exist" in body["last_error"]
    assert body["workflows"] == []


@pytest.mark.parametrize(
    ("sync", "rows", "expected"),
    [
        (None, [], "unknown"),
        ({"last_poll_at": _now(), "last_ok_at": None, "last_error": None}, [], "unknown"),
        (_sync(last_ok_at=_now() - timedelta(seconds=3 * POLL + 60)), [], "unknown"),
        (_sync(), [_workflow(failures_24h=1)], "warn"),
        (_sync(last_error="401 from the n8n API"), [_workflow()], "warn"),
        (_sync(), [_workflow(failures_7d=4)], "ok"),
        (_sync(), [], "ok"),
    ],
    ids=[
        "never-polled",
        "never-succeeded",
        "stale",
        "recent-failure",
        "poll-error",
        "old-failures-only",
        "healthy-empty",
    ],
)
def test_status_matrix(controls_client_as_operator, enabled, fake_db, sync, rows, expected):
    fake_db(n8n_sync_state=[sync] if sync else [], n8n_workflows=rows)
    body = controls_client_as_operator.get("/api/integrations/n8n/workflows").json()
    assert body["status"] == expected


def test_status_for_accepts_iso_strings():
    fresh = (_now() - timedelta(seconds=10)).isoformat()
    assert n8n.status_for({"last_ok_at": fresh, "last_error": None}, POLL, []) == "ok"


# --- executions ---------------------------------------------------------------


def test_executions_carry_links(controls_client_as_operator, enabled, fake_db, calls):
    fake_db(
        n8n_executions=[
            {
                "execution_id": "ex9",
                "status": "error",
                "mode": "trigger",
                "started_at": _now() - timedelta(minutes=5),
                "stopped_at": _now() - timedelta(minutes=4),
                "error_message": "timeout",
            }
        ]
    )
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows/wf1/executions?limit=5")
    assert r.status_code == 200
    [ex] = r.json()["executions"]
    assert ex["execution_id"] == "ex9"
    assert ex["status"] == "error"
    assert ex["error_message"] == "timeout"
    assert isinstance(ex["started_at"], str)
    assert ex["url"] == f"{BASE}/workflow/wf1/executions/ex9"
    [(sql, params)] = calls
    assert "wf1" in params and 5 in params


def test_executions_limit_is_clamped(controls_client_as_operator, enabled, fake_db, calls):
    fake_db(n8n_executions=[])
    controls_client_as_operator.get("/api/integrations/n8n/workflows/wf1/executions?limit=5000")
    [(_, params)] = calls
    assert 100 in params


def test_executions_db_error_is_empty_not_500(controls_client_as_operator, enabled, monkeypatch):
    @contextmanager
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(n8n, "get_connection", boom)
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows/wf1/executions")
    assert r.status_code == 200
    assert r.json()["executions"] == []


# --- configuration that cannot be read ---------------------------------------


def _bad_settings():
    raise ValueError("invalid literal for int() with base 10: 'often'")


def test_unreadable_settings_are_unknown_not_500(controls_client_as_operator, monkeypatch, fake_db):
    monkeypatch.setattr(n8n, "_n8n_settings", _bad_settings)
    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow()])
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unknown"
    assert "often" in body["last_error"]
    assert body["workflows"] == []


def test_a_non_numeric_poll_interval_is_unknown_not_500(
    controls_client_as_operator, monkeypatch, fake_db
):
    from types import SimpleNamespace

    integrations = SimpleNamespace(n8n_enabled=True, n8n_base_url=BASE, n8n_poll_seconds="often")
    monkeypatch.setattr(
        "robothor.settings.get_settings", lambda: SimpleNamespace(integrations=integrations)
    )
    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow()])
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows")
    assert r.status_code == 200
    assert r.json()["status"] == "unknown"


def test_unreadable_settings_give_no_executions(controls_client_as_operator, monkeypatch, fake_db):
    monkeypatch.setattr(n8n, "_n8n_settings", _bad_settings)
    fake_db(n8n_executions=[{"execution_id": "ex1", "status": "error"}])
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows/wf1/executions")
    assert r.status_code == 200
    assert r.json()["executions"] == []


# --- no base URL, or switched off --------------------------------------------


def test_disabled_monitor_lists_no_executions(
    controls_client_as_operator, monkeypatch, fake_db, calls
):
    monkeypatch.setattr(n8n, "_n8n_settings", lambda: (False, BASE, POLL))
    fake_db(n8n_executions=[{"execution_id": "ex1", "status": "error"}])
    r = controls_client_as_operator.get("/api/integrations/n8n/workflows/wf1/executions")
    assert r.status_code == 200
    assert r.json()["executions"] == []
    assert calls == []


def test_no_base_url_means_no_relative_links(controls_client_as_operator, monkeypatch, fake_db):
    monkeypatch.setattr(n8n, "_n8n_settings", lambda: (True, "", POLL))
    fake_db(
        n8n_sync_state=[_sync()],
        n8n_workflows=[_workflow()],
        n8n_executions=[{"execution_id": "ex1", "status": "error"}],
    )
    [wf] = controls_client_as_operator.get("/api/integrations/n8n/workflows").json()["workflows"]
    assert wf["url"] == ""
    executions = controls_client_as_operator.get(
        "/api/integrations/n8n/workflows/wf1/executions"
    ).json()["executions"]
    assert all(ex["url"] == "" for ex in executions)


# --- the last failure links to its execution ---------------------------------


def test_last_failure_links_to_its_execution(controls_client_as_operator, enabled, fake_db, calls):
    fake_db(
        n8n_sync_state=[_sync()],
        n8n_workflows=[
            _workflow(
                failures_24h=1,
                failures_7d=1,
                last_failure_at=_now() - timedelta(hours=1),
                last_error="timeout",
                last_failed_execution_id="ex7",
            )
        ],
    )
    [wf] = controls_client_as_operator.get("/api/integrations/n8n/workflows").json()["workflows"]
    assert wf["last_failure_url"] == f"{BASE}/workflow/wf1/executions/ex7"
    listing_sql = " ".join(sql for sql, _ in calls if "n8n_workflows" in sql)
    assert "ARRAY_AGG(e.execution_id" in listing_sql


def test_no_failure_means_no_failure_link(controls_client_as_operator, enabled, fake_db):
    fake_db(n8n_sync_state=[_sync()], n8n_workflows=[_workflow()])
    [wf] = controls_client_as_operator.get("/api/integrations/n8n/workflows").json()["workflows"]
    assert wf["last_failure_url"] == ""


def test_no_base_url_means_no_failure_link(controls_client_as_operator, monkeypatch, fake_db):
    monkeypatch.setattr(n8n, "_n8n_settings", lambda: (True, "", POLL))
    fake_db(
        n8n_sync_state=[_sync()],
        n8n_workflows=[_workflow(failures_24h=1, last_failed_execution_id="ex7")],
    )
    [wf] = controls_client_as_operator.get("/api/integrations/n8n/workflows").json()["workflows"]
    assert wf["last_failure_url"] == ""
