#!/usr/bin/env python3
"""
Tests for CronContext — shared context loader for all crons.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cron_context import (
    CalendarContext,
    CronContext,
    EmailContext,
    TasksContext,
    parse_datetime,
)


class TestParseDatetime:
    """Test datetime parsing."""

    def test_parse_iso_with_timezone(self):
        dt = parse_datetime("2026-02-05T10:30:00-05:00")
        assert dt is not None
        assert dt.hour == 10
        assert dt.minute == 30

    def test_parse_iso_with_microseconds(self):
        dt = parse_datetime("2026-02-05T10:30:00.123456")
        assert dt is not None
        assert dt.microsecond == 123456

    def test_parse_date_only(self):
        dt = parse_datetime("2026-02-05")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 5

    def test_parse_none(self):
        assert parse_datetime(None) is None

    def test_parse_empty(self):
        assert parse_datetime("") is None

    def test_parse_invalid(self):
        assert parse_datetime("not a date") is None


class TestCalendarContext:
    """Test calendar context loading."""

    @pytest.fixture
    def sample_calendar_data(self):
        now = datetime.now()
        return {
            "lastCheckedAt": now.isoformat(),
            "meetings": [
                {
                    "id": "event1",
                    "title": "Team Standup",
                    "start": (now + timedelta(hours=2)).isoformat(),
                    "end": (now + timedelta(hours=3)).isoformat(),
                    "notifiedAt": None,
                },
                {
                    "id": "event2",
                    "title": "Old Meeting",
                    "start": (now - timedelta(days=30)).isoformat(),
                    "end": (now - timedelta(days=30, hours=-1)).isoformat(),
                    "notifiedAt": None,
                },
            ],
            "changes": [
                {
                    "type": "new",
                    "eventId": "event1",
                    "title": "Team Standup",
                    "detectedAt": now.isoformat(),
                    "reviewedAt": None,
                }
            ],
        }

    def test_load_filters_old_meetings(self, sample_calendar_data, tmp_path):
        log_file = tmp_path / "calendar-log.json"
        log_file.write_text(json.dumps(sample_calendar_data))

        with patch("cron_context.CALENDAR_LOG", log_file):
            ctx = CalendarContext.load(days_back=7, days_forward=14)

        # Should only have the upcoming meeting, not the 30-day old one
        assert len(ctx.meetings) == 1
        assert ctx.meetings[0]["title"] == "Team Standup"

    def test_upcoming_property(self, sample_calendar_data, tmp_path):
        log_file = tmp_path / "calendar-log.json"
        log_file.write_text(json.dumps(sample_calendar_data))

        with patch("cron_context.CALENDAR_LOG", log_file):
            ctx = CalendarContext.load()

        assert len(ctx.upcoming) == 1

    def test_unreviewed_changes(self, sample_calendar_data, tmp_path):
        log_file = tmp_path / "calendar-log.json"
        log_file.write_text(json.dumps(sample_calendar_data))

        with patch("cron_context.CALENDAR_LOG", log_file):
            ctx = CalendarContext.load()

        assert len(ctx.unreviewed_changes) == 1


class TestEmailContext:
    """Test email context loading."""

    @pytest.fixture
    def sample_email_data(self):
        now = datetime.now()
        return {
            "entries": {
                "msg1": {
                    "id": "msg1",
                    "from": "jon@digitalrx.com",
                    "subject": "API Update",
                    "receivedAt": now.isoformat(),
                    "processedAt": now.isoformat(),
                    "urgency": "high",
                    "needsResponse": True,
                    "surfacedAt": None,
                    "respondedAt": None,
                },
                "msg2": {
                    "id": "msg2",
                    "from": "newsletter@spam.com",
                    "subject": "Weekly Digest",
                    "receivedAt": now.isoformat(),
                    "processedAt": now.isoformat(),
                    "urgency": "low",
                    "needsResponse": False,
                    "surfacedAt": now.isoformat(),
                },
                "msg3": {
                    "id": "msg3",
                    "from": "old@example.com",
                    "subject": "Old Email",
                    "receivedAt": (now - timedelta(days=10)).isoformat(),
                    "processedAt": (now - timedelta(days=10)).isoformat(),
                    "urgency": "medium",
                    "needsResponse": False,
                    "surfacedAt": None,
                },
            },
            "lastProcessedAt": now.isoformat(),
        }

    def test_load_filters_old_emails(self, sample_email_data, tmp_path):
        log_file = tmp_path / "email-log.json"
        log_file.write_text(json.dumps(sample_email_data))

        with patch("cron_context.EMAIL_LOG", log_file):
            ctx = EmailContext.load(days_back=3)

        # Should only have 2 recent emails, not the 10-day old one
        assert len(ctx.entries) == 2

    def test_unsurfaced_property(self, sample_email_data, tmp_path):
        log_file = tmp_path / "email-log.json"
        log_file.write_text(json.dumps(sample_email_data))

        with patch("cron_context.EMAIL_LOG", log_file):
            ctx = EmailContext.load(days_back=3)

        # Only msg1 is unsurfaced (msg2 has surfacedAt, msg3 is too old)
        assert len(ctx.unsurfaced) == 1
        assert ctx.unsurfaced[0]["id"] == "msg1"

    def test_needs_response_property(self, sample_email_data, tmp_path):
        log_file = tmp_path / "email-log.json"
        log_file.write_text(json.dumps(sample_email_data))

        with patch("cron_context.EMAIL_LOG", log_file):
            ctx = EmailContext.load(days_back=3)

        assert len(ctx.needs_response) == 1
        assert ctx.needs_response[0]["id"] == "msg1"

    def test_by_urgency(self, sample_email_data, tmp_path):
        log_file = tmp_path / "email-log.json"
        log_file.write_text(json.dumps(sample_email_data))

        with patch("cron_context.EMAIL_LOG", log_file):
            ctx = EmailContext.load(days_back=3)

        by_urg = ctx.by_urgency
        assert len(by_urg["high"]) == 1
        assert len(by_urg["low"]) == 1


class TestTasksContext:
    """Test tasks context loading."""

    @pytest.fixture
    def sample_tasks_data(self):
        now = datetime.now()
        return {
            "tasks": [
                {
                    "id": "task_001",
                    "description": "Call Audi",
                    "status": "pending",
                    "priority": "medium",
                    "dueAt": (now + timedelta(hours=2)).isoformat(),
                    "source": "telegram:philip",
                },
                {
                    "id": "task_002",
                    "description": "Overdue task",
                    "status": "pending",
                    "priority": "high",
                    "dueAt": (now - timedelta(days=1)).isoformat(),
                    "source": "email:123",
                },
                {
                    "id": "VV-123",
                    "description": "Jira ticket",
                    "status": "in_progress",
                    "priority": "medium",
                    "source": "jira:vv",
                },
                {
                    "id": "task_003",
                    "description": "Completed task",
                    "status": "completed",
                    "completedAt": now.isoformat(),
                },
            ]
        }

    def test_load_active_only(self, sample_tasks_data, tmp_path):
        log_file = tmp_path / "tasks.json"
        log_file.write_text(json.dumps(sample_tasks_data))

        with patch("cron_context.TASKS_FILE", log_file):
            ctx = TasksContext.load(active_only=True)

        # Should exclude completed task
        assert len(ctx.tasks) == 3

    def test_pending_property(self, sample_tasks_data, tmp_path):
        log_file = tmp_path / "tasks.json"
        log_file.write_text(json.dumps(sample_tasks_data))

        with patch("cron_context.TASKS_FILE", log_file):
            ctx = TasksContext.load()

        assert len(ctx.pending) == 2

    def test_overdue_property(self, sample_tasks_data, tmp_path):
        log_file = tmp_path / "tasks.json"
        log_file.write_text(json.dumps(sample_tasks_data))

        with patch("cron_context.TASKS_FILE", log_file):
            ctx = TasksContext.load()

        assert len(ctx.overdue) == 1
        assert ctx.overdue[0]["id"] == "task_002"

    def test_from_jira_property(self, sample_tasks_data, tmp_path):
        log_file = tmp_path / "tasks.json"
        log_file.write_text(json.dumps(sample_tasks_data))

        with patch("cron_context.TASKS_FILE", log_file):
            ctx = TasksContext.load()

        assert len(ctx.from_jira) == 1
        assert ctx.from_jira[0]["id"] == "VV-123"


class TestCronContext:
    """Test full context loading."""

    def test_load_returns_all_contexts(self, tmp_path):
        # Create minimal log files
        (tmp_path / "email-log.json").write_text('{"entries": {}}')
        (tmp_path / "calendar-log.json").write_text('{"meetings": [], "changes": []}')
        (tmp_path / "jira-log.json").write_text(
            '{"activeTickets": {}, "pendingActions": [], "syncHistory": []}'
        )
        (tmp_path / "tasks.json").write_text('{"tasks": []}')
        (tmp_path / "contacts.json").write_text('{"contacts": {}}')

        with (
            patch("cron_context.MEMORY_DIR", tmp_path),
            patch("cron_context.EMAIL_LOG", tmp_path / "email-log.json"),
            patch("cron_context.CALENDAR_LOG", tmp_path / "calendar-log.json"),
            patch("cron_context.JIRA_LOG", tmp_path / "jira-log.json"),
            patch("cron_context.TASKS_FILE", tmp_path / "tasks.json"),
            patch("cron_context.CONTACTS_FILE", tmp_path / "contacts.json"),
        ):
            ctx = CronContext.load()

        assert ctx.calendar is not None
        assert ctx.emails is not None
        assert ctx.jira is not None
        assert ctx.tasks is not None
        assert ctx.contacts is not None
        assert ctx.loaded_at is not None

    def test_summary(self, tmp_path):
        (tmp_path / "email-log.json").write_text('{"entries": {}}')
        (tmp_path / "calendar-log.json").write_text('{"meetings": [], "changes": []}')
        (tmp_path / "jira-log.json").write_text(
            '{"activeTickets": {}, "pendingActions": [], "syncHistory": []}'
        )
        (tmp_path / "tasks.json").write_text('{"tasks": []}')
        (tmp_path / "contacts.json").write_text('{"contacts": {}}')

        with (
            patch("cron_context.MEMORY_DIR", tmp_path),
            patch("cron_context.EMAIL_LOG", tmp_path / "email-log.json"),
            patch("cron_context.CALENDAR_LOG", tmp_path / "calendar-log.json"),
            patch("cron_context.JIRA_LOG", tmp_path / "jira-log.json"),
            patch("cron_context.TASKS_FILE", tmp_path / "tasks.json"),
            patch("cron_context.CONTACTS_FILE", tmp_path / "contacts.json"),
        ):
            ctx = CronContext.load()
            summary = ctx.summary()

        assert "calendar" in summary
        assert "emails" in summary
        assert "jira" in summary
        assert "tasks" in summary
        assert "contacts" in summary


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
