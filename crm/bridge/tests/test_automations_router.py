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
from typing import TYPE_CHECKING

import pytest
from routers import automations

if TYPE_CHECKING:
    from pathlib import Path


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


#: A manifest shaped like the primary agent on a real instance: NO
#: ``schedule.cron``, and its two real schedules in the ``heartbeat`` and
#: ``worker`` blocks. Written from the shape of ``docs/agents/main.yaml``, never
#: its content — the crons, names and delivery targets here are invented.
#:
#: This shape is the whole of C1: the engine keys ``agent_schedules`` by JOB id
#: (``<agent>:heartbeat``), so an agent scheduled this way matched neither "has
#: a cron" nor "has a schedule row" and had no card at all.
MAIN_SHAPED_YAML = """
id: orchestrator
name: Orchestrator
description: Runs the instance.
version: 1.0.0
department: core
model:
  primary: test-model
schedule:
  timezone: UTC
  timeout_seconds: 600
delivery:
  mode: announce
  channel: telegram
  to: agent@example.com
heartbeat:
  cron: "0 12,16 * * *"
  timezone: UTC
worker:
  cron: "0 7-22/2 * * *"
  timezone: UTC
"""

#: An ordinary cron'd agent, for the same directory.
PLAIN_YAML = """
id: nightly-report
name: Nightly Report
version: 1.0.0
department: operations
schedule:
  cron: "0 9 * * *"
  timezone: UTC
delivery:
  mode: none
"""


def _manifest_dir(workspace: Path) -> Path:
    directory = workspace / "docs" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


MANIFEST = {
    "id": "invoice-chaser",
    "agent_id": "invoice-chaser",
    "kind": "agent",
    "editable": True,
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
    monkeypatch.setattr(automations, "_latest_runs", lambda ids, tenant_id: state["runs"])
    return state


def _rows(client):
    response = client.get("/api/automations")
    assert response.status_code == 200
    return response.json()["automations"]


# ─── The manifest half, unstubbed ────────────────────────────────────
#
# Every composition test below stubs ``_manifests``. That stub is what hid the
# defect these three exercise: the real reader was looking for a field the
# primary agent on a real instance does not have.


def test_a_manifest_scheduled_only_by_heartbeat_and_worker_still_has_cards(env_workspace):
    """One job, one card — because ``agent_schedules`` is keyed by JOB id.

    ``robothor/engine/schedule_reconcile.py`` derives ``<id>``, ``<id>:heartbeat``
    and ``<id>:worker`` from one manifest and the scheduler writes THOSE as
    ``agent_schedules.agent_id``. An agent whose only schedules are a heartbeat
    and a worker has no ``schedule.cron`` at all, so keying cards by the
    manifest id dropped it entirely — on a real instance that is the primary
    agent, the two rows that fire most often and the two breakers most likely
    to trip.
    """
    (_manifest_dir(env_workspace) / "orchestrator.yaml").write_text(
        MAIN_SHAPED_YAML, encoding="utf-8"
    )
    rows = {row["id"]: row for row in automations._manifests()}

    assert "orchestrator:heartbeat" in rows
    assert "orchestrator:worker" in rows
    # No schedule.cron, so no bare-agent job — exactly what the engine derives.
    assert "orchestrator" not in rows

    heartbeat = rows["orchestrator:heartbeat"]
    assert heartbeat["kind"] == "heartbeat"
    assert heartbeat["agent_id"] == "orchestrator"
    assert heartbeat["cron"] == "0 12,16 * * *"
    assert heartbeat["timezone"] == "UTC"
    # The heartbeat and worker crons are not editable through the manifest
    # form, whose FORM_OWNED_PATHS covers schedule.* only. Saying so beats a
    # form that posts and changes nothing.
    assert heartbeat["editable"] is False


def test_an_ordinary_cron_agent_is_one_card_of_kind_agent(env_workspace):
    (_manifest_dir(env_workspace) / "nightly-report.yaml").write_text(PLAIN_YAML, encoding="utf-8")
    rows = {row["id"]: row for row in automations._manifests()}

    assert rows["nightly-report"]["kind"] == "agent"
    assert rows["nightly-report"]["agent_id"] == "nightly-report"
    assert rows["nightly-report"]["editable"] is True


def test_a_manifest_with_no_schedule_of_any_kind_yields_no_job(env_workspace):
    (_manifest_dir(env_workspace) / "on-demand.yaml").write_text(
        "id: on-demand\nname: On Demand\nversion: 1.0.0\n", encoding="utf-8"
    )
    assert [row["id"] for row in automations._manifests() if row["agent_id"] == "on-demand"] == []


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


def test_a_schedule_row_whose_manifest_will_not_parse_still_gets_a_card(
    controls_client_as_operator, fake_sources
):
    """A YAML typo must never vanish a firing automation.

    ``load_manifest_dir`` puts an unparseable manifest in ``failures``, so it is
    not in ``scan.manifests`` at all — but a blocked reconcile prunes nothing,
    so the engine keeps firing the job it already holds. Dropping the row as
    "no manifest" hid a still-running, still-failing agent, which is the
    2026-08-24 manifest outage in miniature.
    """
    fake_sources["schedules"] = [
        {**SCHEDULE, "agent_id": "ledger-sweeper", "consecutive_errors": 6}
    ]
    fake_sources["manifests"] = []
    fake_sources["runs"] = {}

    row = _rows(controls_client_as_operator)[0]
    assert row["id"] == "ledger-sweeper"
    assert row["manifest_unreadable"] is True
    assert row["cron"] == "0 9 * * *"
    assert row["consecutive_errors"] == 6
    assert row["breaker_tripped"] is True
    # Nothing on the card may invite an edit of a file that will not parse.
    assert row["editable"] is False


def test_a_degraded_card_takes_its_job_kind_from_the_row_id(
    controls_client_as_operator, fake_sources
):
    fake_sources["schedules"] = [{**SCHEDULE, "agent_id": "orchestrator:heartbeat"}]
    fake_sources["manifests"] = []
    fake_sources["runs"] = {}

    row = _rows(controls_client_as_operator)[0]
    assert row["kind"] == "heartbeat"
    assert row["agent_id"] == "orchestrator"


def test_a_manifest_that_parses_is_never_marked_unreadable(
    controls_client_as_operator, fake_sources
):
    assert _rows(controls_client_as_operator)[0]["manifest_unreadable"] is False


def test_one_agents_run_never_lands_on_another_agents_card(
    controls_client_as_operator, fake_sources
):
    fake_sources["runs"] = {"someone-else": dict(RUN)}
    assert _rows(controls_client_as_operator)[0]["last_run"] is None


def test_rows_are_sorted_by_id(controls_client_as_operator, fake_sources):
    fake_sources["manifests"] = [
        {**MANIFEST, "id": "zulu", "agent_id": "zulu", "name": "Zulu"},
        {**MANIFEST, "id": "alpha", "agent_id": "alpha", "name": "Alpha"},
    ]
    fake_sources["schedules"] = []
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
    latest = automations._latest_runs(["invoice-chaser"], "default")
    assert "DISTINCT ON (agent_id)" in seen["sql"]
    assert "started_at DESC" in seen["sql"]
    assert seen["params"] == (["invoice-chaser"], "default")
    assert latest["invoice-chaser"]["status"] == "completed"


def test_latest_runs_asks_nothing_when_there_are_no_agents(monkeypatch):
    monkeypatch.setattr(
        automations, "_query", lambda sql, params: pytest.fail("queried with no agents")
    )
    assert automations._latest_runs([], "default") == {}


def test_latest_run_is_scoped_to_the_tenant_in_the_statement(monkeypatch):
    """A foreign tenant's newer run must never be rendered as this card's.

    ``agent_runs`` is not tenant-scoped by RLS unless the connection carries a
    scope, and the bridge sets none. The schedule query beside this one is
    scoped in the statement for exactly that reason; this one has to be too, or
    another tenant's delivery status and verification verdict end up on the
    card.
    """
    seen: dict = {}

    def _capture(sql, params):
        seen["sql"] = sql
        seen["params"] = params
        return []

    monkeypatch.setattr(automations, "_query", _capture)
    automations._latest_runs(["invoice-chaser"], "probe-tenant")
    assert "tenant_id = %s" in seen["sql"]
    assert seen["params"] == (["invoice-chaser"], "probe-tenant")


def test_the_listing_asks_both_queries_for_the_platform_tenant(
    controls_client_as_operator, monkeypatch
):
    asked: dict = {}

    def _schedules(tenant_id):
        asked["schedules"] = tenant_id
        return []

    def _runs(ids, tenant_id):
        asked["runs"] = tenant_id
        return {}

    monkeypatch.setattr(automations, "_manifests", lambda: [])
    monkeypatch.setattr(automations, "_schedule_rows", _schedules)
    monkeypatch.setattr(automations, "_latest_runs", _runs)

    controls_client_as_operator.get("/api/automations")
    assert asked["schedules"] == automations.PLATFORM_TENANT
    assert asked["runs"] == automations.PLATFORM_TENANT


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


def test_reset_breaker_addresses_the_job_id_the_engine_wrote(
    controls_client_as_operator, monkeypatch
):
    """``main:heartbeat`` is a row id, not a path.

    The scheduler trips its breaker per JOB, so resetting the bare agent id
    would not clear the heartbeat that is actually stopped — and
    ``_safe_id``'s kebab rule refused the colon outright, making the one
    breaker an operator most needs to reset unreachable through this route.
    """
    recorder: dict = {}
    _bind_connection(monkeypatch, recorder, rowcount=1)

    response = controls_client_as_operator.post(
        "/api/automations/orchestrator:heartbeat/reset-breaker"
    )
    assert response.status_code == 200
    assert response.json()["id"] == "orchestrator:heartbeat"
    assert recorder["params"][0] == "orchestrator:heartbeat"


@pytest.mark.parametrize(
    "bad",
    [
        "orchestrator:nightly",  # not a kind the engine derives
        "orchestrator:heartbeat:worker",
        "../../etc/passwd",
        "orchestrator:../worker",
        "Orchestrator",
    ],
)
def test_reset_breaker_still_refuses_anything_that_is_not_a_job_id(
    controls_client_as_operator, monkeypatch, bad
):
    monkeypatch.setattr(
        automations,
        "_reset_breaker",
        lambda agent_id, tenant_id: pytest.fail(f"reached the database with {agent_id!r}"),
    )
    response = controls_client_as_operator.post(
        f"/api/automations/{bad}/reset-breaker", follow_redirects=False
    )
    assert response.status_code in {404, 422}


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
