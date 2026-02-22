"""
Tests for event bus consumers.

Validates consumer handler logic without requiring Redis.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.events.consumers.base import BaseConsumer
from robothor.events.consumers.calendar import CalendarConsumer
from robothor.events.consumers.email import EmailConsumer
from robothor.events.consumers.health import HealthConsumer
from robothor.events.consumers.vision import VisionConsumer

# ─── Base Consumer ───────────────────────────────────────────────────


class ConcreteConsumer(BaseConsumer):
    """Concrete implementation for testing the base class."""

    stream = "test"
    group = "test-group"
    consumer_name = "test-worker"

    def __init__(self):
        super().__init__()
        self.events_handled = []

    def handle(self, event: dict) -> None:
        self.events_handled.append(event)


class TestBaseConsumer:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseConsumer()

    def test_concrete_can_instantiate(self):
        c = ConcreteConsumer()
        assert c.stream == "test"
        assert c.group == "test-group"

    def test_handle_records_events(self):
        c = ConcreteConsumer()
        event = {"type": "test.event", "payload": {"key": "value"}}
        c.handle(event)
        assert len(c.events_handled) == 1
        assert c.events_handled[0]["type"] == "test.event"

    @patch("robothor.events.consumers.base.subscribe")
    def test_run_calls_subscribe(self, mock_subscribe):
        c = ConcreteConsumer()
        c.run(max_iterations=1)
        mock_subscribe.assert_called_once_with(
            "test",
            "test-group",
            "test-worker",
            handler=c.handle,
            batch_size=10,
            block_ms=5000,
            max_iterations=1,
        )

    @patch.dict("os.environ", {"CONSUMER_NAME": "worker-42"})
    def test_consumer_name_from_env(self):
        c = ConcreteConsumer()
        assert c.consumer_name == "worker-42"


# ─── Email Consumer ─────────────────────────────────────────────────


class TestEmailConsumer:
    def test_stream_config(self):
        c = EmailConsumer()
        assert c.stream == "email"
        assert c.group == "email-pipeline"

    def test_processes_new_email(self):
        c = EmailConsumer()
        event = {
            "type": "email.new",
            "payload": {"email_id": "123", "subject": "Test Subject"},
            "source": "email_sync",
        }
        # No hook script configured — should not raise
        c.handle(event)

    @patch("robothor.events.consumers.email.subprocess.Popen")
    @patch.dict("os.environ", {"EMAIL_HOOK_SCRIPT": "/tmp/fake_hook.py"})
    def test_triggers_hook_script(self, mock_popen, tmp_path):
        # Create a fake script so os.path.exists returns True
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            script_path = f.name

        try:
            import os
            os.environ["EMAIL_HOOK_SCRIPT"] = script_path
            c = EmailConsumer()
            event = {
                "type": "email.new",
                "payload": {"email_id": "456"},
                "source": "email_sync",
            }
            c.handle(event)
            mock_popen.assert_called_once()
        finally:
            os.unlink(script_path)
            os.environ.pop("EMAIL_HOOK_SCRIPT", None)

    def test_ignores_unknown_event_type(self):
        c = EmailConsumer()
        event = {"type": "email.unknown", "payload": {}}
        # Should not raise
        c.handle(event)

    def test_handles_classified_event(self):
        c = EmailConsumer()
        event = {
            "type": "email.classified",
            "payload": {"email_id": "789", "classification": "routine"},
        }
        c.handle(event)


# ─── Calendar Consumer ──────────────────────────────────────────────


class TestCalendarConsumer:
    def test_stream_config(self):
        c = CalendarConsumer()
        assert c.stream == "calendar"
        assert c.group == "calendar-monitor"

    def test_handles_conflict(self):
        c = CalendarConsumer()
        event = {
            "type": "calendar.conflict",
            "payload": {"events": [{"title": "Meeting A"}, {"title": "Meeting B"}]},
        }
        c.handle(event)

    def test_handles_cancellation(self):
        c = CalendarConsumer()
        event = {
            "type": "calendar.cancellation",
            "payload": {"title": "Weekly Standup"},
        }
        c.handle(event)

    def test_handles_change(self):
        c = CalendarConsumer()
        event = {
            "type": "calendar.change",
            "payload": {"title": "Planning", "change_type": "reschedule"},
        }
        c.handle(event)

    def test_ignores_unknown_event(self):
        c = CalendarConsumer()
        c.handle({"type": "calendar.unknown", "payload": {}})


# ─── Health Consumer ────────────────────────────────────────────────


class TestHealthConsumer:
    def test_stream_config(self):
        c = HealthConsumer()
        assert c.stream == "health"
        assert c.group == "health-escalation"

    @patch("robothor.events.consumers.health.log_event")
    def test_escalates_degraded_services(self, mock_log):
        c = HealthConsumer()
        event = {
            "type": "service.health",
            "source": "system_health_check",
            "payload": {
                "status": "degraded",
                "services": {
                    "crm": "ok",
                    "memory": "error:connection refused",
                    "impetus_one": "ok",
                },
            },
        }
        c.handle(event)
        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[0][0] == "health.escalation"
        assert "memory" in call_args[0][1]

    @patch("robothor.events.consumers.health.log_event")
    def test_no_escalation_when_healthy(self, mock_log):
        c = HealthConsumer()
        event = {
            "type": "service.health",
            "source": "system_health_check",
            "payload": {
                "status": "ok",
                "services": {"crm": "ok", "memory": "ok"},
            },
        }
        c.handle(event)
        mock_log.assert_not_called()

    @patch("robothor.events.consumers.health.log_event")
    def test_handles_degraded_event_type(self, mock_log):
        c = HealthConsumer()
        event = {
            "type": "service.degraded",
            "payload": {"degraded_services": ["bridge"]},
        }
        c.handle(event)
        mock_log.assert_called_once()

    def test_ignores_unknown_event(self):
        c = HealthConsumer()
        c.handle({"type": "health.unknown", "payload": {}})


# ─── Vision Consumer ────────────────────────────────────────────────


class TestVisionConsumer:
    def test_stream_config(self):
        c = VisionConsumer()
        assert c.stream == "vision"
        assert c.group == "vision-alerts"

    @patch("robothor.events.consumers.vision.log_event")
    def test_alerts_on_unknown_person(self, mock_log):
        c = VisionConsumer()
        event = {
            "type": "vision.person_detected",
            "payload": {"name": "unknown", "confidence": 0.85, "is_known": False},
        }
        c.handle(event)
        mock_log.assert_called_once()
        assert "Unknown person" in mock_log.call_args[0][1]

    @patch("robothor.events.consumers.vision.log_event")
    def test_no_alert_on_known_person(self, mock_log):
        c = VisionConsumer()
        event = {
            "type": "vision.person_detected",
            "payload": {"name": "Philip", "confidence": 0.99, "is_known": True},
        }
        c.handle(event)
        mock_log.assert_not_called()

    def test_handles_motion_event(self):
        c = VisionConsumer()
        c.handle({"type": "vision.motion", "payload": {"zone": "front_door"}})

    def test_handles_analysis_event(self):
        c = VisionConsumer()
        c.handle({"type": "vision.analysis", "payload": {"description": "Empty room"}})

    def test_ignores_unknown_event(self):
        c = VisionConsumer()
        c.handle({"type": "vision.unknown", "payload": {}})


# ─── Consumer Error Handling ────────────────────────────────────────


class TestConsumerErrorHandling:
    def test_handle_error_does_not_ack(self):
        """When handler raises, the event should NOT be acked (bus handles this)."""

        class FailingConsumer(BaseConsumer):
            stream = "test"
            group = "test-group"
            consumer_name = "fail-worker"

            def handle(self, event: dict) -> None:
                raise ValueError("Processing failed")

        c = FailingConsumer()
        with pytest.raises(ValueError, match="Processing failed"):
            c.handle({"type": "test", "payload": {}})
