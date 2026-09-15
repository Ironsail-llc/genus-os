"""Automations answers the question the Helm could not: did it RUN, did it get
DELIVERED, and did it COMPLETE.

Three sources, one row. The manifest says what the automation IS (name, cron,
timezone, delivery target), ``agent_schedules`` says what the scheduler BELIEVES
(next fire, consecutive errors), and the newest ``agent_runs`` row says what
actually HAPPENED. Every test here is about the seam between them, because each
one alone has been on a screen for months and none of them alone answered the
question.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from routers import automations


class _Cursor:
    """A psycopg2 cursor stand-in that records what the route asked for."""

    def __init__(self, recorder: dict, rowcount: int = 1):
        self._recorder = recorder
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self._recorder["sql"] = sql
        self._recorder["params"] = params

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _bind_connection(monkeypatch, recorder: dict, rowcount: int = 1):
    cursor = _Cursor(recorder, rowcount)

    class _Conn:
        def cursor(self, **kwargs):
            return cursor

    @contextmanager
    def _fake():
        yield _Conn()

    monkeypatch.setattr(automations, "get_connection", _fake)


MANIFEST = {
    "id": "invoice-chaser",
    "name": "Invoice Chaser",
    "description": "Chases unpaid invoices.",
    "version": "1.0.0",
    "department": "operations",
    "cron": "0 9 * * *",
    "timezone": "UTC",
    "enabled": True,
    "delivery": "announce",
    "model": "test-model",
    "delivery_channel": "telegram",
    "delivery_to": "agent@example.com",
}

SCHEDULE = {
    "agent_id": "invoice-chaser",
    "enabled": True,
    "cron_expr": "0 9 * * *",
    "timezone": "UTC",
    "next_run_at": "2026-06-16T09:00:00+00:00",
    "consecutive_errors": 0,
    "delivery_mode": "announce",
    "delivery_channel": "telegram",
    "delivery_to": "agent@example.com",
}

RUN = {
    "agent_id": "invoice-chaser",
    "id": "11111111-1111-4111-8111-111111111111",
    "started_at": "2026-06-15T09:00:00+00:00",
    "status": "completed",
    "duration_ms": 4200,
    "delivery_status": "delivered",
    "delivered_at": "2026-06-15T09:01:00+00:00",
    "delivery_channel": "telegram",
    "delivery_mode": "announce",
    "verified_status": "verified",
    "outcome_assessment": "successful",
}


@pytest.fixture
def fake_sources(monkeypatch):
    """The three halves, each replaceable on its own."""
    state = {
        "manifests": [dict(MANIFEST)],
        "schedules": [dict(SCHEDULE)],
        "runs": {"invoice-chaser": dict(RUN)},
    }
    monkeypatch.setattr(automations, "_manifests", lambda: state["manifests"])
    monkeypatch.setattr(automations, "_schedule_rows", lambda tenant_id: state["schedules"])
    monkeypatch.setattr(automations, "_latest_runs", lambda ids: state["runs"])
    return state


def _rows(client):
    response = client.get("/api/automations")
    assert response.status_code == 200
    return response.json()["automations"]


# ─── The gate ────────────────────────────────────────────────────────


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/automations").status_code == 403


def test_reset_requires_operator(controls_client_as_viewer):
    response = controls_client_as_viewer.post("/api/automations/invoice-chaser/reset-breaker")
    assert response.status_code == 403


def test_another_tenants_operator_is_refused(controls_client_as_other_tenant_owner):
    assert controls_client_as_other_tenant_owner.get("/api/automations").status_code == 403


# ─── Composition ─────────────────────────────────────────────────────


def test_composes_manifest_schedule_and_latest_run(controls_client_as_operator, fake_sources):
    row = _rows(controls_client_as_operator)[0]
    assert row["id"] == "invoice-chaser"
    assert row["name"] == "Invoice Chaser"
    assert row["kind"] == "agent"
    assert row["cron"] == "0 9 * * *"
    assert row["timezone"] == "UTC"
    assert row["enabled"] is True
    assert row["next_run_at"] == "2026-06-16T09:00:00+00:00"
    assert row["delivery"] == {
        "mode": "announce",
        "channel": "telegram",
        "to": "agent@example.com",
    }
    assert row["last_run"]["id"] == RUN["id"]
    assert row["last_run"]["status"] == "completed"
    assert row["last_run"]["duration_ms"] == 4200
    assert row["last_run"]["delivery_status"] == "delivered"
    assert row["last_run"]["delivered_at"] == RUN["delivered_at"]
    assert row["last_run"]["delivery_channel"] == "telegram"
    assert row["last_run"]["verified_status"] == "verified"
    assert row["last_run"]["outcome_assessment"] == "successful"


def test_a_manifest_with_no_schedule_row_is_still_an_automation(
    controls_client_as_operator, fake_sources
):
    """The scheduler has not reconciled yet. The card still has to appear —
    "my agent has a cron and is nowhere in the Helm" is the confusion this
    whole view exists to end."""
    fake_sources["schedules"] = []
    fake_sources["runs"] = {}
    row = _rows(controls_client_as_operator)[0]
    assert row["cron"] == "0 9 * * *"
    assert row["next_run_at"] is None
    assert row["last_run"] is None
    assert row["consecutive_errors"] == 0
    assert row["breaker_tripped"] is False


def test_a_manifest_with_no_cron_and_no_schedule_row_is_not_an_automation(
    controls_client_as_operator, fake_sources
):
    fake_sources["manifests"] = [{**MANIFEST, "cron": ""}]
    fake_sources["schedules"] = []
    assert _rows(controls_client_as_operator) == []


def test_a_cronless_manifest_the_scheduler_still_holds_is_listed(
    controls_client_as_operator, fake_sources
):
    """A cron removed from the manifest but not yet reconciled away is exactly
    the drift an operator needs to see, not the one to hide."""
    fake_sources["manifests"] = [{**MANIFEST, "cron": "", "timezone": ""}]
    rows = _rows(controls_client_as_operator)
    assert len(rows) == 1
    assert rows[0]["cron"] == "0 9 * * *"
    assert rows[0]["timezone"] == "UTC"


def test_a_schedule_row_with_no_manifest_is_dropped(controls_client_as_operator, fake_sources):
    fake_sources["schedules"] = [{**SCHEDULE, "agent_id": "retired-agent"}]
    fake_sources["manifests"] = []
    assert _rows(controls_client_as_operator) == []


def test_one_agents_run_never_lands_on_another_agents_card(
    controls_client_as_operator, fake_sources
):
    fake_sources["runs"] = {"someone-else": dict(RUN)}
    assert _rows(controls_client_as_operator)[0]["last_run"] is None


def test_rows_are_sorted_by_id(controls_client_as_operator, fake_sources):
    fake_sources["manifests"] = [
        {**MANIFEST, "id": "zulu", "name": "Zulu"},
        {**MANIFEST, "id": "alpha", "name": "Alpha"},
    ]
    assert [row["id"] for row in _rows(controls_client_as_operator)] == ["alpha", "zulu"]


# ─── Latest-run selection ────────────────────────────────────────────


def test_latest_run_selection_is_newest_per_agent(monkeypatch):
    """The "latest run" is picked in SQL, one row per agent, newest first.

    Asserted on the statement rather than a fixture because a LIMIT 1 per agent
    in Python would have to fetch every run of every agent to do it.
    """
    seen: dict = {}

    def _capture(sql, params):
        seen["sql"] = sql
        seen["params"] = params
        return [dict(RUN)]

    monkeypatch.setattr(automations, "_query", _capture)
    latest = automations._latest_runs(["invoice-chaser"])
    assert "DISTINCT ON (agent_id)" in seen["sql"]
    assert "started_at DESC" in seen["sql"]
    assert seen["params"] == (["invoice-chaser"],)
    assert latest["invoice-chaser"]["status"] == "completed"


def test_latest_runs_asks_nothing_when_there_are_no_agents(monkeypatch):
    monkeypatch.setattr(
        automations, "_query", lambda sql, params: pytest.fail("queried with no agents")
    )
    assert automations._latest_runs([]) == {}


# ─── The breaker ─────────────────────────────────────────────────────


def test_threshold_is_the_schedulers_own_constant(controls_client_as_operator, fake_sources):
    from robothor.engine.scheduler import CIRCUIT_BREAKER_THRESHOLD

    assert _rows(controls_client_as_operator)[0]["breaker_threshold"] == CIRCUIT_BREAKER_THRESHOLD


def test_breaker_trips_at_the_threshold_and_not_before(
    controls_client_as_operator, fake_sources, monkeypatch
):
    monkeypatch.setattr(automations, "_threshold", lambda: 5)

    fake_sources["schedules"] = [{**SCHEDULE, "consecutive_errors": 4}]
    row = _rows(controls_client_as_operator)[0]
    assert row["consecutive_errors"] == 4
    assert row["breaker_tripped"] is False

    fake_sources["schedules"] = [{**SCHEDULE, "consecutive_errors": 5}]
    assert _rows(controls_client_as_operator)[0]["breaker_tripped"] is True

    fake_sources["schedules"] = [{**SCHEDULE, "consecutive_errors": 9}]
    assert _rows(controls_client_as_operator)[0]["breaker_tripped"] is True


def test_reset_breaker_zeroes_the_row_for_this_tenant(controls_client_as_operator, monkeypatch):
    recorder: dict = {}
    _bind_connection(monkeypatch, recorder, rowcount=1)

    response = controls_client_as_operator.post("/api/automations/invoice-chaser/reset-breaker")
    assert response.status_code == 200
    assert response.json() == {"id": "invoice-chaser", "consecutive_errors": 0}

    assert "consecutive_errors = 0" in recorder["sql"]
    assert "agent_schedules" in recorder["sql"]
    assert "tenant_id = %s" in recorder["sql"]
    assert recorder["params"][0] == "invoice-chaser"
    assert recorder["params"][1] == automations.PLATFORM_TENANT


def test_reset_breaker_404s_when_the_row_belongs_to_another_tenant(
    controls_client_as_operator, monkeypatch
):
    """No row matched ``agent_id AND tenant_id``. That is 404 and never a
    silent 200: an operator told "reset" about a row they did not touch would
    go on waiting for an agent that is still tripped."""
    recorder: dict = {}
    _bind_connection(monkeypatch, recorder, rowcount=0)

    response = controls_client_as_operator.post("/api/automations/invoice-chaser/reset-breaker")
    assert response.status_code == 404


def test_reset_breaker_is_audited(controls_client_as_operator, monkeypatch):
    events: list[tuple] = []
    _bind_connection(monkeypatch, {}, rowcount=1)
    monkeypatch.setattr(
        automations,
        "audited",
        lambda request, event_type, **kwargs: events.append((event_type, kwargs)),
    )

    controls_client_as_operator.post("/api/automations/invoice-chaser/reset-breaker")
    assert events and events[0][1]["action"] == "invoice-chaser"


# ─── What must never be on the wire ──────────────────────────────────


def test_the_payload_carries_no_credential_shaped_field(controls_client_as_operator, fake_sources):
    body = controls_client_as_operator.get("/api/automations").text.lower()
    for word in ("api_key", "apikey", "token", "password", "secret", "bearer"):
        assert word not in body


def test_the_payload_carries_no_filesystem_path(controls_client_as_operator, fake_sources):
    """``_summary`` never carries one, and this route must not reintroduce it:
    the manifest directory is the box's layout, not the operator's business."""
    body = controls_client_as_operator.get("/api/automations").text
    assert "/home/" not in body
    assert "docs/agents" not in body
