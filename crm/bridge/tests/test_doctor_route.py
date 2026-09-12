"""``GET /api/doctor`` -- the Helm's Health view and the setup wizard's strip.

Two things are pinned. The gate, because this route enumerates what is wrong
with the appliance and which credentials it holds: a viewer, a non-operator
human and an agent token are all refused by the same primitive every other
operator surface uses. And the shape, because the dashboard renders a readiness
payload and a doctor payload with one component -- the top-level ``status`` and
``checks`` keys mirror ``health_contract.readiness_response`` deliberately.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.doctor.runner import CheckResult, DoctorReport

ONE_PASS = DoctorReport(
    results=[
        CheckResult(
            id="db.connect",
            title="PostgreSQL is reachable",
            category="database",
            severity="required",
            status="pass",
            detail="connected to robothor_memory",
            fixable=False,
        )
    ]
)

ONE_REQUIRED_FAILURE = DoctorReport(
    results=[
        CheckResult(
            id="db.rbac_service_role",
            title="The 'service' role is seeded",
            category="database",
            severity="required",
            status="fail",
            detail="the 'service' role has no permission rules",
            fixable=True,
        )
    ]
)


@pytest.fixture
def healthy():
    with patch("routers.health.run_sync", return_value=ONE_PASS) as stub:
        yield stub


@pytest.fixture
def degraded():
    with patch("routers.health.run_sync", return_value=ONE_REQUIRED_FAILURE) as stub:
        yield stub


@pytest.fixture
def anonymous_client():
    """No Authorization header at all -- the shape a browser that has never
    signed in sends."""
    from bridge_service import app
    from fastapi.testclient import TestClient

    return TestClient(app)


# ── the gate ─────────────────────────────────────────────────────────────────


def test_a_viewer_cannot_read_the_doctor(controls_client_as_viewer, healthy) -> None:
    assert controls_client_as_viewer.get("/api/doctor").status_code == 403


def test_a_non_operator_human_cannot_read_the_doctor(controls_client_as_user, healthy) -> None:
    assert controls_client_as_user.get("/api/doctor").status_code == 403


def test_an_agent_token_cannot_read_the_doctor(controls_client_as_service, healthy) -> None:
    """An agent asking what is wrong with its own host is not a use case."""
    assert controls_client_as_service.get("/api/doctor").status_code == 403


def test_an_unauthenticated_request_is_refused(anonymous_client, healthy) -> None:
    assert anonymous_client.get("/api/doctor").status_code in (401, 403)


def test_an_operator_may_read_it(controls_client_as_operator, healthy) -> None:
    assert controls_client_as_operator.get("/api/doctor").status_code == 200


def test_an_admin_may_read_it(controls_client_as_admin, healthy) -> None:
    assert controls_client_as_admin.get("/api/doctor").status_code == 200


# ── the shape ────────────────────────────────────────────────────────────────


def test_the_payload_mirrors_the_readiness_contract(controls_client_as_operator, healthy) -> None:
    body = controls_client_as_operator.get("/api/doctor").json()
    assert body["status"] == "ok"
    assert isinstance(body["checks"], list)
    assert body["summary"] == {
        "required_failed": 0,
        "recommended_failed": 0,
        "passed": 1,
        "skipped": 0,
    }


def test_every_check_row_carries_the_documented_keys(controls_client_as_operator, healthy) -> None:
    row = controls_client_as_operator.get("/api/doctor").json()["checks"][0]
    assert set(row) == {"id", "title", "category", "severity", "status", "detail", "fixable"}


def test_a_required_failure_reads_as_degraded(controls_client_as_operator, degraded) -> None:
    body = controls_client_as_operator.get("/api/doctor").json()
    assert body["status"] == "degraded"
    assert body["summary"]["required_failed"] == 1
    assert body["checks"][0]["fixable"] is True


def test_a_degraded_instance_still_answers_two_hundred(
    controls_client_as_operator, degraded
) -> None:
    """Unlike /ready, this route is a REPORT, not a probe. Answering 503 would
    make a load balancer or a dashboard treat 'the doctor found something' as
    'the bridge is down'."""
    assert controls_client_as_operator.get("/api/doctor").status_code == 200


# ── how it runs ──────────────────────────────────────────────────────────────


def test_the_route_never_repairs_anything(controls_client_as_operator, healthy) -> None:
    """A GET must not seed a role or apply a migration, whoever is asking."""
    controls_client_as_operator.get("/api/doctor")
    ctx = healthy.call_args.args[0]
    assert ctx.fix is False
    assert ctx.dry_run is False


def test_the_route_uses_the_same_five_second_per_check_budget(
    controls_client_as_operator, healthy
) -> None:
    controls_client_as_operator.get("/api/doctor")
    assert healthy.call_args.args[0].timeout_s == 5.0


def test_the_route_runs_offline_so_a_dashboard_poll_costs_nothing(
    controls_client_as_operator, healthy
) -> None:
    """This is what the Helm's Health view polls. Online, every refresh would
    make a paid completion through the fleet's default model, a getMe against
    Telegram and a fork of the host script -- two operator tabs at 30s is
    5,760 paid calls a day from a dashboard refresh."""
    controls_client_as_operator.get("/api/doctor")
    assert healthy.call_args.args[0].offline is True


def test_the_whole_run_is_bounded_not_just_each_check(controls_client_as_operator, healthy) -> None:
    """26 checks x 5s is over two minutes on one of asyncio's default-executor
    workers, and the tunnel in front of the bridge gives up at 100s. A handful
    of concurrent operator requests would starve every other to_thread route."""
    controls_client_as_operator.get("/api/doctor")
    total = healthy.call_args.args[0].total_timeout_s
    assert total is not None
    assert total <= 60.0


def test_a_doctor_that_could_not_run_is_reported_rather_than_a_500(
    controls_client_as_operator,
) -> None:
    errored = DoctorReport(errored=True, error_detail="RuntimeError: the registry is broken")
    with patch("routers.health.run_sync", return_value=errored):
        response = controls_client_as_operator.get("/api/doctor")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert "the registry is broken" in body["error"]


def test_a_doctor_that_raises_is_a_report_not_a_traceback(controls_client_as_operator) -> None:
    with patch("routers.health.run_sync", side_effect=RuntimeError("boom")):
        response = controls_client_as_operator.get("/api/doctor")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
