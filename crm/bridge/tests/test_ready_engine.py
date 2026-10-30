"""The bridge's ``/ready`` and ``/api/doctor`` report whether it reaches the engine.

Most of what an operator does in the Helm is proxied to the engine. A bridge
that cannot reach it answers every one of those calls with ``502 engine
unavailable`` -- and before this check its ``/ready`` stayed green the whole
time, because nothing it probed was the engine.

The engine is deliberately NOT a readiness gate. Pulling every bridge out of
its Service because the engine is down would take sign-in, the CRM and the
Health view down with it -- the very surfaces that say what is wrong. So
``/ready`` keeps answering 200 and carries a ``degraded`` marker and the engine
result, and ``/api/doctor`` names the address it tried and how it failed.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.doctor.runner import CheckResult, DoctorReport

ENGINE = "http://engine.example.com:18800"
MEMORY_READY = "http://localhost:9099/ready"

ONE_PASS = DoctorReport(
    results=[
        CheckResult(
            id="db.connect",
            title="PostgreSQL is reachable",
            category="database",
            severity="required",
            status="pass",
            detail="connected",
            fixable=False,
        )
    ]
)


def _response(status_code: int) -> MagicMock:
    return MagicMock(spec=httpx.Response, status_code=status_code)


def _router(engine):
    """A ``get`` that answers memory with 200 and the engine with ``engine``:
    a status code, or an exception to raise."""

    async def get(url, **_kwargs):
        if url.startswith(ENGINE):
            if isinstance(engine, BaseException):
                raise engine
            return _response(engine)
        return _response(200)

    return AsyncMock(side_effect=get)


@pytest.fixture(autouse=True)
def _pinned_environment(monkeypatch):
    # The sso_secret check reads the process environment; pinned so the other
    # checks are "ok" here and in CI alike (see test_bridge_api.py).
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret")
    monkeypatch.setenv("ROBOTHOR_ENGINE_URL", ENGINE)


@pytest.fixture(autouse=True)
def _no_memoised_report():
    from routers.health import reset_doctor_cache

    reset_doctor_cache()
    yield
    reset_doctor_cache()


async def _ready(test_client):
    with patch("robothor.crm.dal.check_health", return_value={"status": "ok"}):
        return await test_client.get("/ready")


# ── /ready ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_live_engine_reads_ok_and_not_degraded(test_client, mock_http_client):
    mock_http_client.get = _router(200)

    r = await _ready(test_client)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["engine"] == "ok"
    assert "degraded" not in body
    assert body["checks"] == {"crm": "ok", "memory": "ok", "sso_secret": "ok"}
    mock_http_client.get.assert_any_await(f"{ENGINE}/live")
    mock_http_client.get.assert_any_await(MEMORY_READY)


@pytest.mark.asyncio
async def test_a_refused_engine_is_degraded_but_still_ready(test_client, mock_http_client, caplog):
    mock_http_client.get = _router(httpx.ConnectError("connection refused"))

    with caplog.at_level(logging.WARNING):
        r = await _ready(test_client)

    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["engine"] == "unreachable:ConnectError"
    # The engine is not one of the gating checks: the others still read ok.
    assert body["checks"] == {"crm": "ok", "memory": "ok", "sso_secret": "ok"}
    assert "Readiness check engine failed: ConnectError" in caplog.text


@pytest.mark.asyncio
async def test_an_engine_answering_503_is_unavailable_the_same_way(
    test_client, mock_http_client, caplog
):
    mock_http_client.get = _router(503)

    with caplog.at_level(logging.WARNING):
        r = await _ready(test_client)

    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["engine"] == "unreachable:HTTP 503"
    assert "Readiness check engine failed: HTTP 503" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_gate_and_a_dead_engine_still_answer_503(test_client, mock_http_client):
    """The engine never makes the bridge ready either: a real gate still wins."""
    mock_http_client.get = _router(httpx.ConnectError("connection refused"))

    with patch("robothor.crm.dal.check_health", return_value={"status": "error", "error": "x"}):
        r = await test_client.get("/ready")

    assert r.status_code == 503
    assert r.json()["degraded"] is True


@pytest.mark.asyncio
async def test_live_still_checks_nothing(test_client, mock_http_client):
    mock_http_client.get = AsyncMock(side_effect=AssertionError("dependency called"))

    r = await test_client.get("/live")

    assert r.status_code == 200
    assert "engine" not in r.json()


# ── /api/doctor ──────────────────────────────────────────────────────────────


def _engine_row(body: dict) -> dict:
    rows = [row for row in body["checks"] if row["id"] == "bridge.engine"]
    assert len(rows) == 1, body["checks"]
    return rows[0]


def test_the_doctor_lists_the_engine_it_reached(controls_client_as_operator, mock_http_client):
    mock_http_client.get = _router(200)

    with patch("routers.health.run_sync", return_value=ONE_PASS):
        body = controls_client_as_operator.get("/api/doctor").json()

    row = _engine_row(body)
    assert set(row) == {"id", "title", "category", "severity", "status", "detail", "fixable"}
    assert row["status"] == "pass"
    assert ENGINE in row["detail"]
    assert body["status"] == "ok"


def test_the_doctor_names_the_url_and_the_failure_class(
    controls_client_as_operator, mock_http_client
):
    mock_http_client.get = _router(httpx.ConnectError("connection refused"))

    with patch("routers.health.run_sync", return_value=ONE_PASS):
        response = controls_client_as_operator.get("/api/doctor")

    assert response.status_code == 200
    body = response.json()
    row = _engine_row(body)
    assert row["status"] == "fail"
    assert f"{ENGINE}/live" in row["detail"]
    assert "ConnectError" in row["detail"]
    assert body["status"] == "degraded"


def test_the_doctor_reports_an_http_status_as_the_failure(
    controls_client_as_operator, mock_http_client
):
    mock_http_client.get = _router(403)

    with patch("routers.health.run_sync", return_value=ONE_PASS):
        row = _engine_row(controls_client_as_operator.get("/api/doctor").json())

    assert row["status"] == "fail"
    assert "HTTP 403" in row["detail"]


def test_the_doctor_url_is_scheme_host_and_port_only(
    monkeypatch, controls_client_as_operator, mock_http_client
):
    """Whatever else the setting carries -- credentials, a path, a query -- the
    report names only where it knocked."""
    monkeypatch.setenv(
        "ROBOTHOR_ENGINE_URL", "http://alice:s3cret@engine.example.com:18800/x?t=qz9-token-value"
    )
    mock_http_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("routers.health.run_sync", return_value=ONE_PASS):
        response = controls_client_as_operator.get("/api/doctor")

    row = _engine_row(response.json())
    assert row["detail"].startswith(f"{ENGINE}/live")
    assert "s3cret" not in response.text
    assert "alice" not in response.text
    assert "qz9-token-value" not in response.text
