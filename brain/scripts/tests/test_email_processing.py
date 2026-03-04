#!/usr/bin/env python3
"""
Tests for Email Processing.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from email_processing import (
    EmailDecision,
    ResponseType,
    Urgency,
    estimate_urgency,
    get_context_for_email,
    needs_response_heuristic,
    update_email_log,
)


class TestEmailDecision:
    """Test EmailDecision dataclass."""

    def test_default_values(self):
        decision = EmailDecision(email_id="test123")

        assert decision.urgency == Urgency.LOW
        assert decision.needs_response is False
        assert decision.response_type == ResponseType.NONE

    def test_to_dict(self):
        decision = EmailDecision(
            email_id="test123",
            urgency=Urgency.HIGH,
            needs_response=True,
            response_type=ResponseType.AUTO,
            summary="Test email summary",
        )

        d = decision.to_dict()

        assert d["urgency"] == "high"
        assert d["needs_response"] is True
        assert d["response_type"] == "auto"
        assert d["summary"] == "Test email summary"

    def test_escalate_with_reason(self):
        decision = EmailDecision(
            email_id="test123",
            urgency=Urgency.CRITICAL,
            response_type=ResponseType.ESCALATE,
            escalate_reason="Security concern",
        )

        d = decision.to_dict()

        assert d["response_type"] == "escalate"
        assert d["escalate_reason"] == "Security concern"


class TestEstimateUrgency:
    """Test urgency estimation heuristics."""

    def test_critical_keywords(self):
        email = {"subject": "URGENT: Server down", "snippet": "Production is offline"}
        assert estimate_urgency(email) == Urgency.CRITICAL

        email = {"subject": "Security breach detected", "snippet": "Immediate action needed"}
        assert estimate_urgency(email) == Urgency.CRITICAL

    def test_high_keywords(self):
        email = {"subject": "Important: Deadline today", "snippet": "Please review"}
        assert estimate_urgency(email) == Urgency.HIGH

        email = {"subject": "Invoice payment required", "snippet": "Payment due"}
        assert estimate_urgency(email) == Urgency.HIGH

    def test_low_senders(self):
        email = {
            "subject": "URGENT Sale!",  # Would be critical, but...
            "from": "newsletter@spam.com",  # ...it's from a newsletter
            "snippet": "Buy now!",
        }
        assert estimate_urgency(email) == Urgency.LOW

        email = {
            "subject": "Your order update",
            "from": "noreply@amazon.com",
            "snippet": "Status update",
        }
        assert estimate_urgency(email) == Urgency.LOW

    def test_medium_default_for_real_email(self):
        email = {
            "subject": "Quick question",
            "from": "jon@digitalrx.com",
            "snippet": "Hey, wanted to follow up",
        }
        assert estimate_urgency(email) == Urgency.MEDIUM

    def test_empty_email(self):
        email = {}
        assert estimate_urgency(email) == Urgency.LOW


class TestNeedsResponseHeuristic:
    """Test response-needed heuristics."""

    def test_question_needs_response(self):
        email = {"subject": "Quick question?", "snippet": "When is the meeting"}
        assert needs_response_heuristic(email) is True

    def test_action_request_needs_response(self):
        email = {"subject": "Request", "snippet": "Can you send me the docs?"}
        assert needs_response_heuristic(email) is True

        email = {"subject": "Follow up", "snippet": "Please let me know your thoughts"}
        assert needs_response_heuristic(email) is True

    def test_notification_no_response(self):
        email = {"subject": "Your order shipped", "snippet": "Tracking number: 123"}
        assert needs_response_heuristic(email) is False

    def test_fyi_no_response(self):
        email = {"subject": "FYI: Meeting notes", "snippet": "Attached are the notes from today"}
        assert needs_response_heuristic(email) is False


class TestGetContextForEmail:
    """Test context building for emails."""

    def test_finds_related_meeting(self):
        email = {"from": "jon@digitalrx.com", "subject": "About the API discussion"}

        # Create mock context
        mock_ctx = MagicMock()
        mock_ctx.contacts.find_by_email.return_value = {"name": "Jon", "email": "jon@digitalrx.com"}
        mock_ctx.calendar.upcoming = [
            {
                "id": "meeting1",
                "title": "API Integration Discussion",
                "attendees": ["jon@digitalrx.com", "philip@ironsail.ai"],
            }
        ]
        mock_ctx.tasks.pending = []

        context = get_context_for_email(email, mock_ctx)

        assert context["sender_contact"] is not None
        assert len(context["related_meetings"]) == 1

    def test_finds_related_task(self):
        email = {"from": "team@company.com", "subject": "HubSpot payment update"}

        mock_ctx = MagicMock()
        mock_ctx.contacts.find_by_email.return_value = None
        mock_ctx.calendar.upcoming = []
        mock_ctx.tasks.pending = [
            {"id": "task_001", "description": "HubSpot payment needs to be retried"}
        ]

        context = get_context_for_email(email, mock_ctx)

        assert len(context["related_tasks"]) == 1


class TestUpdateEmailLog:
    """Test email log updates."""

    def test_update_creates_entry(self, tmp_path):
        log_file = tmp_path / "email-log.json"
        log_file.write_text('{"entries": {}}')

        email = {
            "id": "msg123",
            "from": "test@example.com",
            "to": "robothor@ironsail.ai",
            "subject": "Test email",
            "date": "2026-02-05T10:00:00-05:00",
        }

        decision = EmailDecision(
            email_id="msg123",
            urgency=Urgency.MEDIUM,
            needs_response=True,
            summary="Test email about something",
        )

        with patch("email_processing.EMAIL_LOG", log_file):
            update_email_log(email, decision)

        data = json.loads(log_file.read_text())

        assert "msg123" in data["entries"]
        entry = data["entries"]["msg123"]
        assert entry["urgency"] == "medium"
        assert entry["needs_response"] is True
        assert entry["surfacedAt"] is None

    def test_update_sets_escalated_flag(self, tmp_path):
        log_file = tmp_path / "email-log.json"
        log_file.write_text('{"entries": {}}')

        email = {"id": "urgent123", "from": "boss@company.com", "subject": "URGENT"}

        decision = EmailDecision(
            email_id="urgent123",
            urgency=Urgency.CRITICAL,
            response_type=ResponseType.ESCALATE,
            escalate_reason="Needs CEO decision",
        )

        with patch("email_processing.EMAIL_LOG", log_file):
            update_email_log(email, decision)

        data = json.loads(log_file.read_text())
        entry = data["entries"]["urgent123"]

        assert entry["escalated"] is True
        assert entry["escalate_reason"] == "Needs CEO decision"


class TestEvalSuite:
    """
    Evaluation suite with real-world email scenarios.

    These tests verify the heuristics produce sensible results
    for common email types.
    """

    @pytest.mark.parametrize(
        "email,expected_urgency,expected_response",
        [
            # Critical - system issues
            (
                {
                    "subject": "URGENT: Production database down",
                    "from": "alerts@ironsail.ai",
                    "snippet": "Database connection failed",
                },
                Urgency.CRITICAL,
                False,  # Alert emails don't typically need a reply
            ),
            # High - business critical (heuristic doesn't catch "overdue" without question/action words)
            (
                {
                    "subject": "Invoice overdue - payment required",
                    "from": "billing@vendor.com",
                    "snippet": "Your invoice is past due",
                },
                Urgency.HIGH,
                False,  # Heuristic misses this - agent will catch it
            ),
            # Medium - normal business with question
            (
                {
                    "subject": "Re: Project timeline",
                    "from": "colleague@company.com",
                    "snippet": "Can you review the updated schedule?",
                },
                Urgency.MEDIUM,
                True,  # "Can you" triggers response
            ),
            # Low - newsletters
            (
                {
                    "subject": "Weekly AI News Digest",
                    "from": "newsletter@technews.com",
                    "snippet": "This week in AI...",
                },
                Urgency.LOW,
                False,
            ),
            # Low - automated notifications
            (
                {
                    "subject": "Your GitHub notification",
                    "from": "noreply@github.com",
                    "snippet": "You have new activity",
                },
                Urgency.LOW,
                False,
            ),
        ],
    )
    def test_email_scenarios(self, email, expected_urgency, expected_response):
        assert estimate_urgency(email) == expected_urgency
        assert needs_response_heuristic(email) == expected_response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
