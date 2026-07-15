"""Fleet answers: what can each agent DO vs what did it do. The honesty carry-over
is `findings`: an agent holding exec with no allowlist is flagged like an inert
control — a capability without a constraint is a finding, not a fact."""

import pytest
from routers import fleet


@pytest.fixture
def fake_fleet(monkeypatch):
    manifests = [
        {
            "id": "main",
            "name": "Main",
            "department": "core",
            "model": {"primary": "m"},
            "sandbox": "host",
            "delivery": {"mode": "announce"},
            "tools_allowed": ["exec", "web_fetch"],
            "exec_allowlist": ["git status"],
        },
        {
            "id": "loose",
            "name": "Loose",
            "department": "core",
            "model": {"primary": "m"},
            "sandbox": "host",
            "delivery": {"mode": "none"},
            "tools_allowed": ["exec"],
            "exec_allowlist": [],
        },
    ]
    schedule_rows = {
        "main": {
            "enabled": True,
            "next_run_at": None,
            "last_run_at": None,
            "last_status": "completed",
            "consecutive_errors": 0,
        },
    }
    run_stats = {"main": {"runs_7d": 5, "failures_7d": 1}}
    monkeypatch.setattr(fleet, "_load_manifests", lambda: manifests)
    monkeypatch.setattr(fleet, "_schedule_rows", lambda: schedule_rows)
    monkeypatch.setattr(fleet, "_run_stats", lambda: run_stats)


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/fleet").status_code == 403


def test_list_returns_all_agents_for_operator(controls_client_as_operator, fake_fleet):
    r = controls_client_as_operator.get("/api/fleet")
    assert r.status_code == 200
    ids = {a["agent_id"] for a in r.json()}
    assert ids == {"main", "loose"}


def test_unconstrained_exec_agent_is_flagged(controls_client_as_operator, fake_fleet):
    by_id = {a["agent_id"]: a for a in controls_client_as_operator.get("/api/fleet").json()}
    assert by_id["main"]["findings"] == []  # exec + allowlist → clean
    codes = {f["code"] for f in by_id["loose"]["findings"]}
    assert "EXEC_NO_ALLOWLIST" in codes  # exec + no allowlist → flagged


def test_detail_404_for_unknown_agent(controls_client_as_operator, fake_fleet):
    assert controls_client_as_operator.get("/api/fleet/nope").status_code == 404


def test_fleet_router_is_read_only():
    methods = {m for route in fleet.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
