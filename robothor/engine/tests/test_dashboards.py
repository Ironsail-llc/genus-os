"""Tests for the dashboard module (replaces brain/ Node.js servers)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Data loaders ─────────────────────────────────────────────────────────────


class TestDataLoaders:
    """Test memory file loaders."""

    def test_load_json_missing_file(self):
        from robothor.engine.dashboards.data import _load_json

        result = _load_json(Path("/nonexistent/file.json"))
        assert result is None

    def test_load_json_valid(self, tmp_path):
        from robothor.engine.dashboards.data import _load_json

        f = tmp_path / "test.json"
        f.write_text(json.dumps({"key": "value"}))
        result = _load_json(f)
        assert result == {"key": "value"}

    def test_load_json_invalid(self, tmp_path):
        from robothor.engine.dashboards.data import _load_json

        f = tmp_path / "bad.json"
        f.write_text("not json")
        result = _load_json(f)
        assert result is None

    def test_get_emails_empty(self):
        from robothor.engine.dashboards.data import get_emails

        with patch("robothor.engine.dashboards.data._load_json", return_value=None):
            result = get_emails()
            assert result["stats"]["total"] == 0
            assert result["emails"] == []

    def test_get_emails_with_data(self):
        from robothor.engine.dashboards.data import get_emails

        mock_data = {
            "lastCheckedAt": "2026-03-30T10:00:00Z",
            "entries": {
                "1": {
                    "from": "test@example.com",
                    "subject": "Hello",
                    "receivedAt": "2026-03-30T09:00:00Z",
                    "urgency": "high",
                },
                "2": {
                    "from": "other@example.com",
                    "subject": "World",
                    "reviewedAt": "2026-03-30T09:30:00Z",
                },
            },
        }
        with patch("robothor.engine.dashboards.data._load_json", return_value=mock_data):
            result = get_emails()
            assert result["stats"]["total"] == 2
            assert result["stats"]["unread"] == 1
            assert result["stats"]["urgent"] == 1

    def test_get_tasks_empty(self):
        from robothor.engine.dashboards.data import get_tasks

        with patch("robothor.engine.dashboards.data._load_json", return_value=None):
            result = get_tasks()
            assert result["stats"]["total"] == 0

    def test_get_tasks_with_data(self):
        from robothor.engine.dashboards.data import get_tasks

        mock_data = {
            "tasks": [
                {"id": "1", "status": "pending", "description": "Task 1"},
                {"id": "2", "status": "in_progress", "description": "Task 2"},
                {"id": "3", "status": "completed", "description": "Task 3"},
            ]
        }
        with patch("robothor.engine.dashboards.data._load_json", return_value=mock_data):
            result = get_tasks()
            assert result["stats"]["total"] == 3
            assert result["stats"]["pending"] == 1
            assert result["stats"]["inProgress"] == 1

    def test_get_worker_handoff_empty(self):
        from robothor.engine.dashboards.data import get_worker_handoff

        with patch("robothor.engine.dashboards.data._load_json", return_value=None):
            result = get_worker_handoff()
            assert result["escalations"]["total"] == 0

    def test_get_cron_status(self):
        from robothor.engine.dashboards.data import get_cron_status

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="*/5 * * * * /path/to/script.py\n"
            )
            result = get_cron_status()
            assert len(result["systemCrons"]) == 1
            assert result["systemCrons"][0]["command"] == "script.py"


# ── Theme ────────────────────────────────────────────────────────────────────


class TestTheme:
    """Test brand theme."""

    def test_brand_css_contains_variables(self):
        from robothor.engine.dashboards.theme import brand_css

        css = brand_css()
        assert "--r-bg:" in css
        assert "--r-primary:" in css
        assert "--r-font:" in css

    def test_brand_css_contains_base(self):
        from robothor.engine.dashboards.theme import brand_css

        css = brand_css()
        assert ".glass" in css
        assert ".bg-gradient" in css


# ── Services ─────────────────────────────────────────────────────────────────


class TestServices:
    """Test service health checks."""

    def test_service_status_to_dict(self):
        from robothor.engine.dashboards.services import ServiceStatus

        s = ServiceStatus(id="test", name="Test", icon="T", status="up", response_ms=42)
        d = s.to_dict()
        assert d["id"] == "test"
        assert d["status"] == "up"
        assert d["responseMs"] == 42

    def test_get_overall_status_all_up(self):
        from robothor.engine.dashboards.services import ServiceStatus, get_overall_status

        services = [
            ServiceStatus(id="a", name="A", icon="A", status="up"),
            ServiceStatus(id="b", name="B", icon="B", status="up"),
        ]
        msg, cls = get_overall_status(services)
        assert msg == "All Systems Operational"
        assert cls == "ok"

    def test_get_overall_status_partial(self):
        from robothor.engine.dashboards.services import ServiceStatus, get_overall_status

        services = [
            ServiceStatus(id="a", name="A", icon="A", status="up"),
            ServiceStatus(id="b", name="B", icon="B", status="down"),
        ]
        msg, cls = get_overall_status(services)
        assert msg == "Partial Outage"
        assert cls == "partial"

    def test_get_overall_status_major(self):
        from robothor.engine.dashboards.services import ServiceStatus, get_overall_status

        services = [
            ServiceStatus(id="a", name="A", icon="A", status="down"),
            ServiceStatus(id="b", name="B", icon="B", status="down"),
            ServiceStatus(id="c", name="C", icon="C", status="down"),
        ]
        msg, cls = get_overall_status(services)
        assert msg == "Major Outage"
        assert cls == "major"

    def test_freshness_check_no_file(self):
        from robothor.engine.dashboards.services import _check_freshness

        status, ms, detail = _check_freshness("/nonexistent.json", "field", 15, 60)
        assert status == "down"

    def test_systemd_check(self):
        from robothor.engine.dashboards.services import _check_systemd

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="active\n")
            status, ms = _check_systemd("cloudflared")
            assert status == "up"

    def test_get_uptime(self):
        from robothor.engine.dashboards.services import get_uptime

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="up 2 weeks, 3 days, 5 hours\n")
            result = get_uptime()
            assert "2 weeks" in result


# ── Router (integration-style) ───────────────────────────────────────────────


class TestRouter:
    """Test dashboard routes exist and are wired correctly."""

    def test_router_has_expected_routes(self):
        from robothor.engine.dashboards.router import router

        paths = {r.path for r in router.routes}
        assert "/dashboards/status" in paths
        assert "/dashboards/status/api" in paths
        assert "/dashboards/ops" in paths
        assert "/dashboards/ops/api" in paths
        assert "/dashboards/homepage" in paths
        assert "/dashboards/privacy" in paths

    def test_template_files_exist(self):
        from robothor.engine.dashboards.router import TEMPLATE_DIR

        assert (TEMPLATE_DIR / "status.html").exists()
        assert (TEMPLATE_DIR / "ops.html").exists()
        assert (TEMPLATE_DIR / "homepage.html").exists()
        assert (TEMPLATE_DIR / "privacy.html").exists()
        assert (TEMPLATE_DIR / "work-with-me.html").exists()
        assert (TEMPLATE_DIR / "now.html").exists()
        assert (TEMPLATE_DIR / "docs.html").exists()
        assert (TEMPLATE_DIR / "subdomains.html").exists()
        assert (TEMPLATE_DIR / "contact.html").exists()

    def test_privacy_renders(self):
        from robothor.engine.dashboards.router import TEMPLATE_DIR

        html = (TEMPLATE_DIR / "privacy.html").read_text()
        assert "Privacy Policy" in html
        assert "robothor@ironsail.ai" in html
