"""Runs answers: what happened in this run — every step, block, and cost."""

import pytest
from routers import runs


@pytest.fixture
def fake_runs(monkeypatch):
    monkeypatch.setattr(
        runs,
        "_list_runs",
        lambda agent, limit: [
            {"id": "r1", "agent_id": "main", "status": "completed", "total_cost_usd": 0.01},
        ],
    )
    monkeypatch.setattr(
        runs,
        "_get_run",
        lambda run_id: (
            {"id": "r1", "agent_id": "main", "status": "completed"} if run_id == "r1" else None
        ),
    )
    monkeypatch.setattr(
        runs,
        "_get_steps",
        lambda run_id: [
            {"step_number": 1, "step_type": "tool_call", "tool_name": "exec"},
        ],
    )
    monkeypatch.setattr(
        runs,
        "_get_guardrail_events",
        lambda run_id: [
            {"guardrail_name": "exec_allowlist_strict", "action": "blocked", "tool_name": "exec"},
        ],
    )


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/runs").status_code == 403


def test_list_returns_runs(controls_client_as_operator, fake_runs):
    r = controls_client_as_operator.get("/api/runs")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "r1"


def test_list_clamps_limit(controls_client_as_operator, monkeypatch):
    seen = {}

    def _fake_list_runs(agent, limit):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(runs, "_list_runs", _fake_list_runs)
    controls_client_as_operator.get("/api/runs?limit=9999")
    assert seen["limit"] == 200


def test_detail_bundles_steps_and_events(controls_client_as_operator, fake_runs):
    body = controls_client_as_operator.get("/api/runs/r1").json()
    assert body["run"]["id"] == "r1"
    assert body["steps"][0]["tool_name"] == "exec"
    assert body["guardrail_events"][0]["action"] == "blocked"


def test_detail_404(controls_client_as_operator, fake_runs):
    assert controls_client_as_operator.get("/api/runs/nope").status_code == 404


def test_runs_router_is_read_only():
    methods = {m for route in runs.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}


def test_run_columns_carry_the_delivery_and_verification_truth():
    """The engine has been writing these four columns for months and the Runs
    list did not select them, so "completed" on screen could mean a run whose
    answer never left the box. Asserted on the column list because that is the
    single place the omission happened."""
    for column in ("delivered_at", "delivery_channel", "delivery_mode", "verified_status"):
        assert column in runs._RUN_COLUMNS
