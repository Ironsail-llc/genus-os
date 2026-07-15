"""Health answers: is the box OK — WAL archiving, backups, failed units, disks.
Honesty rule: a stale backup or failed unit is a flagged status, never green;
a probe that itself fails degrades to 'unknown', never a false-healthy."""

import pytest
from routers import system_health as sh


@pytest.fixture
def fake_probes(monkeypatch):
    monkeypatch.setattr(
        sh,
        "_wal_status",
        lambda: {
            "archived_count": 10,
            "failed_count": 0,
            "last_archived_time": None,
            "status": "ok",
        },
    )
    monkeypatch.setattr(
        sh,
        "_backup_status",
        lambda: [
            {
                "unit": "robothor-backup-local.timer",
                "last_trigger": None,
                "age_hours": 2.0,
                "status": "ok",
            }
        ],
    )
    monkeypatch.setattr(sh, "_failed_units", lambda: [])
    monkeypatch.setattr(
        sh,
        "_disk_status",
        lambda: [{"mount": "/", "used_pct": 40.0, "free_gb": 100.0, "status": "ok"}],
    )


def test_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/health/system").status_code == 403


def test_returns_all_sections(controls_client_as_operator, fake_probes):
    body = controls_client_as_operator.get("/api/health/system").json()
    assert set(body) >= {"wal", "backups", "failed_units", "disks", "generated_at"}
    assert body["wal"]["status"] == "ok"


def test_a_failing_probe_degrades_to_unknown_not_500(controls_client_as_operator, monkeypatch):
    def boom():
        raise RuntimeError("systemctl missing")

    monkeypatch.setattr(
        sh,
        "_wal_status",
        lambda: {
            "archived_count": 0,
            "failed_count": 0,
            "last_archived_time": None,
            "status": "ok",
        },
    )
    monkeypatch.setattr(sh, "_backup_status", boom)
    monkeypatch.setattr(sh, "_failed_units", lambda: [])
    monkeypatch.setattr(sh, "_disk_status", lambda: [])
    r = controls_client_as_operator.get("/api/health/system")
    assert r.status_code == 200
    assert r.json()["backups"]["status"] == "unknown"


def test_health_router_is_read_only():
    methods = {m for route in sh.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
