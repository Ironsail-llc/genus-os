#!/usr/bin/env python3
"""
Tests for system_health_check.py.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from system_health_check import (
    check_email_data_quality,
    resolve_health_escalations,
    write_escalation,
)


class TestEmailDataQuality:
    """Test email data quality detection."""

    def test_detects_recent_null_entries(self, tmp_path):
        email_log = tmp_path / "email-log.json"
        now = datetime.now()
        email_log.write_text(
            json.dumps(
                {
                    "entries": {
                        "bad1": {
                            "fetchedAt": now.isoformat(),
                            "from": None,
                            "subject": None,
                        },
                        "bad2": {
                            "fetchedAt": now.isoformat(),
                            "from": None,
                            "subject": None,
                        },
                    }
                }
            )
        )

        with patch("system_health_check.EMAIL_LOG_PATH", email_log):
            results = check_email_data_quality()

        assert len(results) == 1
        assert results[0]["status"] == "CRITICAL"
        assert "2/2" in results[0]["detail"]

    def test_all_ok_when_content_present(self, tmp_path):
        email_log = tmp_path / "email-log.json"
        now = datetime.now()
        email_log.write_text(
            json.dumps(
                {
                    "entries": {
                        "good1": {
                            "fetchedAt": now.isoformat(),
                            "from": "philip@ironsail.ai",
                            "subject": "Test",
                        },
                    }
                }
            )
        )

        with patch("system_health_check.EMAIL_LOG_PATH", email_log):
            results = check_email_data_quality()

        assert len(results) == 1
        assert results[0]["status"] == "ok"

    def test_ignores_old_entries(self, tmp_path):
        email_log = tmp_path / "email-log.json"
        old = (datetime.now() - timedelta(days=3)).isoformat()
        email_log.write_text(
            json.dumps(
                {
                    "entries": {
                        "old1": {
                            "fetchedAt": old,
                            "from": None,
                            "subject": None,
                        },
                    }
                }
            )
        )

        with patch("system_health_check.EMAIL_LOG_PATH", email_log):
            results = check_email_data_quality()

        assert results[0]["status"] == "ok"
        assert "No recent entries" in results[0]["detail"]

    def test_handles_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with patch("system_health_check.EMAIL_LOG_PATH", missing):
            results = check_email_data_quality()

        assert results[0]["status"] == "CRITICAL"


class TestEscalation:
    """Test escalation writing to worker-handoff.json."""

    def test_escalation_writes_to_handoff(self, tmp_path):
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text(
            json.dumps(
                {
                    "lastRunAt": "2026-02-13T14:30:00",
                    "escalations": [],
                }
            )
        )

        failures = [
            {"name": "systemd:robothor-crm", "status": "CRITICAL", "detail": "inactive"},
            {"name": "http:bridge", "status": "CRITICAL", "detail": "Connection refused"},
        ]

        with patch("system_health_check.HANDOFF_PATH", handoff):
            write_escalation(failures)

        data = json.loads(handoff.read_text())
        assert len(data["escalations"]) == 1
        esc = data["escalations"][0]
        assert esc["source"] == "health_check"
        assert "2 CRITICAL" in esc["summary"]
        assert esc["resolvedAt"] is None

    def test_no_duplicate_escalations(self, tmp_path):
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text(
            json.dumps(
                {
                    "escalations": [
                        {
                            "id": "health-20260213-1400",
                            "source": "health_check",
                            "summary": "1 CRITICAL failures detected",
                            "resolvedAt": None,
                        }
                    ],
                }
            )
        )

        failures = [
            {"name": "systemd:robothor-crm", "status": "CRITICAL", "detail": "inactive"},
        ]

        with patch("system_health_check.HANDOFF_PATH", handoff):
            write_escalation(failures)

        data = json.loads(handoff.read_text())
        # Should still be 1 escalation (updated, not duplicated)
        assert len(data["escalations"]) == 1
        assert "1 CRITICAL" in data["escalations"][0]["summary"]
        assert data["escalations"][0].get("updatedAt") is not None

    def test_does_not_duplicate_resolved_escalation(self, tmp_path):
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text(
            json.dumps(
                {
                    "escalations": [
                        {
                            "id": "health-20260213-1200",
                            "source": "health_check",
                            "summary": "old failure",
                            "resolvedAt": "2026-02-13T13:00:00",
                        }
                    ],
                }
            )
        )

        failures = [
            {"name": "redis", "status": "CRITICAL", "detail": "Connection refused"},
        ]

        with patch("system_health_check.HANDOFF_PATH", handoff):
            write_escalation(failures)

        data = json.loads(handoff.read_text())
        # Resolved one stays, new one added
        assert len(data["escalations"]) == 2


class TestAutoResolution:
    """Test that open health_check escalations get resolved when all checks pass."""

    def test_resolves_open_health_escalation(self, tmp_path):
        """Regression: race condition at 7am restart left escalation open forever."""
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text(
            json.dumps(
                {
                    "escalations": [
                        {
                            "id": "health-20260216-0700",
                            "source": "health_check",
                            "summary": "2 CRITICAL failures detected",
                            "detail": ["systemd:robothor-vision: deactivating"],
                            "resolvedAt": None,
                        }
                    ],
                }
            )
        )

        with patch("system_health_check.HANDOFF_PATH", handoff):
            resolve_health_escalations()

        data = json.loads(handoff.read_text())
        assert data["escalations"][0]["resolvedAt"] is not None

    def test_leaves_non_health_escalations_untouched(self, tmp_path):
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text(
            json.dumps(
                {
                    "escalations": [
                        {
                            "id": "health-20260216-0700",
                            "source": "health_check",
                            "resolvedAt": None,
                        },
                        {
                            "id": "email-abc123",
                            "source": "email",
                            "resolvedAt": None,
                        },
                    ],
                }
            )
        )

        with patch("system_health_check.HANDOFF_PATH", handoff):
            resolve_health_escalations()

        data = json.loads(handoff.read_text())
        # health_check resolved
        assert data["escalations"][0]["resolvedAt"] is not None
        # email escalation untouched
        assert data["escalations"][1]["resolvedAt"] is None

    def test_skips_already_resolved_escalations(self, tmp_path):
        handoff = tmp_path / "worker-handoff.json"
        original_ts = "2026-02-15T14:00:00"
        handoff.write_text(
            json.dumps(
                {
                    "escalations": [
                        {
                            "id": "health-20260215-0900",
                            "source": "health_check",
                            "resolvedAt": original_ts,
                        },
                    ],
                }
            )
        )

        with patch("system_health_check.HANDOFF_PATH", handoff):
            resolve_health_escalations()

        data = json.loads(handoff.read_text())
        # resolvedAt should be unchanged (not overwritten)
        assert data["escalations"][0]["resolvedAt"] == original_ts

    def test_noop_when_no_handoff_file(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with patch("system_health_check.HANDOFF_PATH", missing):
            # Should not raise
            resolve_health_escalations()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
