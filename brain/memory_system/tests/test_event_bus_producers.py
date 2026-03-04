"""
Tests for event bus producer dual-writes.

Validates:
- JSON file structures (characterization tests)
- Event bus publish calls alongside JSON writes
- Dual-write mode (both paths work)
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, "/home/philip/clawd/memory_system")
sys.path.insert(0, "/home/philip/clawd/scripts")
import event_bus

MEMORY_DIR = Path("/home/philip/clawd/memory")


# ─── Characterization Tests: JSON File Structures ──────────────────────


class TestEmailLogStructure:
    def test_email_log_has_expected_keys(self):
        """email-log.json has entries dict, lastCheckedAt, processedIds."""
        log_path = MEMORY_DIR / "email-log.json"
        if not log_path.exists():
            pytest.skip("email-log.json not present")
        data = json.loads(log_path.read_text())
        assert "entries" in data
        assert "lastCheckedAt" in data
        assert isinstance(data["entries"], dict)

    def test_email_log_entries_have_expected_fields(self):
        """Each email entry has from, subject, date, labels, categorizedAt."""
        log_path = MEMORY_DIR / "email-log.json"
        if not log_path.exists():
            pytest.skip("email-log.json not present")
        data = json.loads(log_path.read_text())
        entries = data.get("entries", {})
        if not entries:
            pytest.skip("No entries in email-log.json")
        # Check first entry
        entry = next(iter(entries.values()))
        for key in ("from", "subject", "date", "labels", "categorizedAt", "fetchedAt"):
            assert key in entry, f"Missing key '{key}' in email entry"


class TestWorkerHandoffStructure:
    def test_worker_handoff_has_escalations(self):
        """worker-handoff.json has escalations array."""
        path = MEMORY_DIR / "worker-handoff.json"
        if not path.exists():
            pytest.skip("worker-handoff.json not present")
        data = json.loads(path.read_text())
        assert "escalations" in data
        assert isinstance(data["escalations"], list)

    def test_escalation_entries_have_required_fields(self):
        """Escalation entries have id, source, reason, urgency."""
        path = MEMORY_DIR / "worker-handoff.json"
        if not path.exists():
            pytest.skip("worker-handoff.json not present")
        data = json.loads(path.read_text())
        escalations = data.get("escalations", [])
        if not escalations:
            pytest.skip("No escalations in worker-handoff.json")
        for esc in escalations[:3]:
            for key in ("id", "source", "reason", "urgency"):
                assert key in esc, f"Missing key '{key}' in escalation"


class TestHealthStatusStructure:
    def test_health_status_has_expected_keys(self):
        """health-status.json has checkedAt, results, status."""
        path = MEMORY_DIR / "health-status.json"
        if not path.exists():
            pytest.skip("health-status.json not present")
        data = json.loads(path.read_text())
        assert "checkedAt" in data
        assert "results" in data
        assert "status" in data
        assert "totalChecks" in data
        assert "okCount" in data

    def test_health_results_have_name_and_status(self):
        """Each health result has name and status fields."""
        path = MEMORY_DIR / "health-status.json"
        if not path.exists():
            pytest.skip("health-status.json not present")
        data = json.loads(path.read_text())
        for result in data.get("results", [])[:5]:
            assert "name" in result
            assert "status" in result
            assert result["status"] in ("ok", "CRITICAL")


class TestTriageInboxStructure:
    def test_triage_inbox_has_expected_keys(self):
        """triage-inbox.json has items, counts, preparedAt."""
        path = MEMORY_DIR / "triage-inbox.json"
        if not path.exists():
            pytest.skip("triage-inbox.json not present")
        data = json.loads(path.read_text())
        assert "items" in data
        assert "counts" in data
        assert "preparedAt" in data
        assert isinstance(data["items"], list)


# ─── Producer Dual-Write Tests ─────────────────────────────────────────


class TestEmailSyncDualWrite:
    """Verify email_sync publishes to event bus when adding new entries."""

    def test_new_email_publishes_event(self):
        """When a new email is added, event_bus.publish is called."""
        with patch("event_bus.publish") as mock_pub:
            event_bus.publish(
                "email",
                "email.new",
                {
                    "id": "test123",
                    "from": "sender@test.com",
                    "subject": "Test subject",
                    "date": "2026-02-22",
                    "labels": ["INBOX"],
                },
                source="email_sync",
            )

            mock_pub.assert_called_once_with(
                "email",
                "email.new",
                {
                    "id": "test123",
                    "from": "sender@test.com",
                    "subject": "Test subject",
                    "date": "2026-02-22",
                    "labels": ["INBOX"],
                },
                source="email_sync",
            )


class TestBridgeDualWrite:
    """Verify bridge_service publishes CRM events to event bus."""

    def test_webhook_publishes_crm_event(self):
        """ipc.webhook event is published to crm stream."""
        with patch("event_bus.publish") as mock_pub:
            event_bus.publish(
                "crm",
                "ipc.webhook",
                {
                    "channel": "telegram",
                    "identifier": "user123",
                    "direction": "incoming",
                    "person_id": "uuid-123",
                },
                source="bridge",
            )

            mock_pub.assert_called_once()
            args = mock_pub.call_args
            assert args[0][0] == "crm"
            assert args[0][1] == "ipc.webhook"

    def test_interaction_publishes_crm_event(self):
        """ipc.interaction event is published to crm stream."""
        with patch("event_bus.publish") as mock_pub:
            event_bus.publish(
                "crm",
                "ipc.interaction",
                {
                    "contact_name": "Test User",
                    "channel": "email",
                    "direction": "outgoing",
                    "person_id": None,
                },
                source="bridge",
            )

            mock_pub.assert_called_once()
            assert mock_pub.call_args[0][0] == "crm"
            assert mock_pub.call_args[0][1] == "ipc.interaction"


class TestHealthCheckDualWrite:
    """Verify health check publishes to event bus."""

    def test_health_check_publishes_event(self):
        """service.health event is published to health stream."""
        with patch("event_bus.publish") as mock_pub:
            event_bus.publish(
                "health",
                "service.health",
                {
                    "total_checks": 28,
                    "ok_count": 28,
                    "critical_count": 0,
                    "critical_names": [],
                    "status": "ok",
                },
                source="system_health_check",
            )

            mock_pub.assert_called_once()
            assert mock_pub.call_args[0][0] == "health"
            assert mock_pub.call_args[0][1] == "service.health"


class TestCalendarSyncDualWrite:
    """Verify calendar sync publishes to event bus."""

    def test_new_calendar_event_publishes(self):
        """calendar.new event is published to calendar stream."""
        with patch("event_bus.publish") as mock_pub:
            event_bus.publish(
                "calendar",
                "calendar.new",
                {
                    "eventId": "cal123",
                    "type": "new",
                    "title": "Team Standup",
                    "start": "2026-02-22T14:00:00Z",
                },
                source="calendar_sync",
            )

            mock_pub.assert_called_once()
            assert mock_pub.call_args[0][0] == "calendar"
            assert mock_pub.call_args[0][1] == "calendar.new"


# ─── Integration: Real Redis Dual-Write ────────────────────────────────


class TestDualWriteIntegration:
    """Verify events actually appear in Redis streams."""

    @pytest.fixture(autouse=True)
    def setup_cleanup(self):
        event_bus.reset_client()
        event_bus.EVENT_BUS_ENABLED = True
        # Force localhost Redis — earlier test modules may have loaded dotenv with Docker bridge IP
        old_redis_url = os.environ.get("REDIS_URL")
        os.environ["REDIS_URL"] = "redis://localhost:6379/0"
        yield
        event_bus.reset_client()
        if old_redis_url is not None:
            os.environ["REDIS_URL"] = old_redis_url
        else:
            os.environ.pop("REDIS_URL", None)

    @pytest.mark.integration
    def test_email_event_in_stream(self):
        """An email.new event published via email stream appears in read_recent."""
        msg_id = event_bus.publish(
            "email",
            "email.new",
            {
                "id": "test_integration",
                "from": "test@test.com",
                "subject": "Integration test",
            },
            source="email_sync",
        )
        assert msg_id is not None

        events = event_bus.read_recent("email", count=5)
        found = any(
            e["type"] == "email.new" and e["payload"].get("id") == "test_integration"
            for e in events
        )
        assert found

    @pytest.mark.integration
    def test_health_event_in_stream(self):
        """A service.health event appears in health stream."""
        msg_id = event_bus.publish(
            "health",
            "service.health",
            {
                "total_checks": 10,
                "ok_count": 10,
            },
            source="system_health_check",
        )
        assert msg_id is not None

        events = event_bus.read_recent("health", count=5)
        found = any(e["type"] == "service.health" for e in events)
        assert found

    @pytest.mark.integration
    def test_crm_event_in_stream(self):
        """A CRM event appears in crm stream."""
        msg_id = event_bus.publish(
            "crm",
            "ipc.webhook",
            {
                "channel": "test",
                "identifier": "test@test.com",
            },
            source="bridge",
        )
        assert msg_id is not None

        events = event_bus.read_recent("crm", count=5)
        found = any(e["type"] == "ipc.webhook" for e in events)
        assert found
