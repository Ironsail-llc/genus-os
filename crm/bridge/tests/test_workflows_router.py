"""Workflows answers: what multi-agent flows exist and what have they run.
Bridge-side and operator-scoped (the engine's /api/workflows* is neither).
Lists workflows that have run at least once — the honesty limitation is surfaced
in the tab copy, not hidden."""

import pytest
from routers import workflows


@pytest.fixture
def fake_workflows(monkeypatch):
    monkeypatch.setattr(
        workflows,
        "_list_workflows",
        lambda: [
            {
                "workflow_id": "intel",
                "runs": 3,
                "last_run_at": None,
                "last_status": "completed",
                "failures": 0,
            },
        ],
    )
    monkeypatch.setattr(
        workflows,
        "_workflow_runs",
        lambda wid, limit: [{"id": "wr1", "status": "completed"}] if wid == "intel" else [],
    )


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/workflows").status_code == 403


def test_list_returns_workflows(controls_client_as_operator, fake_workflows):
    r = controls_client_as_operator.get("/api/workflows")
    assert r.status_code == 200
    assert r.json()[0]["workflow_id"] == "intel"


def test_runs_history(controls_client_as_operator, fake_workflows):
    r = controls_client_as_operator.get("/api/workflows/intel/runs")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "wr1"


def test_workflows_router_is_read_only():
    methods = {m for route in workflows.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
