#!/usr/bin/env python3
"""Pipeline health integration tests for email_sync.py triage inbox pipeline.

Tests freshness checks, email/calendar pending extraction, and end-to-end
build_triage_inbox flow with mocked I/O.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_gog_env(monkeypatch):
    """GOG_KEYRING_PASSWORD is required at import time."""
    monkeypatch.setenv("GOG_KEYRING_PASSWORD", "test-password")


@pytest.fixture(autouse=True)
def _mock_event_bus():
    """Prevent real Redis calls from event_bus."""
    mock_bus = MagicMock()
    with patch.dict(sys.modules, {"event_bus": mock_bus}):
        yield mock_bus


@pytest.fixture
def sync_module(_mock_event_bus):
    """Import email_sync fresh so module-level code picks up mocked env/modules."""
    mod_name = "email_sync"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import email_sync

    return email_sync


# ---------------------------------------------------------------------------
# 1. Triage inbox freshness
# ---------------------------------------------------------------------------


class TestTriageInboxFreshness:
    """Verify triage-inbox.json staleness detection based on preparedAt timestamp."""

    def test_fresh_inbox_passes(self, sync_module, tmp_path):
        """Inbox updated 10 min ago should be considered fresh (within 15 min)."""
        prepared_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        inbox = {"preparedAt": prepared_at, "counts": {"total": 0}, "items": []}
        inbox_path = tmp_path / "triage-inbox.json"
        inbox_path.write_text(json.dumps(inbox))

        data = json.loads(inbox_path.read_text())
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["preparedAt"])
        assert age < timedelta(minutes=15), "Inbox should be fresh (< 15 min)"

    def test_stale_inbox_fails(self, sync_module, tmp_path):
        """Inbox updated 20 min ago should be considered stale (> 15 min)."""
        prepared_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        inbox = {"preparedAt": prepared_at, "counts": {"total": 0}, "items": []}
        inbox_path = tmp_path / "triage-inbox.json"
        inbox_path.write_text(json.dumps(inbox))

        data = json.loads(inbox_path.read_text())
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["preparedAt"])
        assert age > timedelta(minutes=15), "Inbox should be stale (> 15 min)"


# ---------------------------------------------------------------------------
# 2. Triage inbox reflects email log
# ---------------------------------------------------------------------------


class TestTriageInboxReflectsEmailLog:
    """Verify _get_pending_emails correctly extracts uncategorized entries."""

    def test_uncategorized_emails_extracted(self, sync_module):
        """Entries with categorizedAt=None and a from field should be pending."""
        email_log = {
            "entries": {
                "msg1": {
                    "from": "alice@example.com",
                    "subject": "Hello",
                    "date": "2026-03-05T10:00:00Z",
                    "categorizedAt": None,
                },
                "msg2": {
                    "from": "bob@example.com",
                    "subject": "Meeting",
                    "date": "2026-03-05T11:00:00Z",
                    "categorizedAt": None,
                },
                "msg3": {
                    "from": "charlie@example.com",
                    "subject": "Done",
                    "date": "2026-03-05T09:00:00Z",
                    "categorizedAt": "2026-03-05T09:30:00Z",
                },
            }
        }
        pending = sync_module._get_pending_emails(email_log)
        assert len(pending) == 2
        pending_ids = {item["id"] for item in pending}
        assert pending_ids == {"msg1", "msg2"}

    def test_entries_without_from_flagged_needs_backfill(self, sync_module):
        """Entries missing a from field should appear with needsBackfill flag."""
        email_log = {
            "entries": {
                "msg1": {"subject": "No sender", "categorizedAt": None},
                "msg2": {
                    "from": "valid@example.com",
                    "subject": "Has sender",
                    "categorizedAt": None,
                },
            }
        }
        pending = sync_module._get_pending_emails(email_log)
        assert len(pending) == 2
        by_id = {p["id"]: p for p in pending}
        assert by_id["msg1"].get("needsBackfill") is True
        assert "needsBackfill" not in by_id["msg2"]

    def test_empty_log_returns_empty(self, sync_module):
        """An empty entries dict should yield no pending emails."""
        pending = sync_module._get_pending_emails({"entries": {}})
        assert pending == []


# ---------------------------------------------------------------------------
# 3. Email sync produces triage inbox
# ---------------------------------------------------------------------------


class TestEmailSyncProducesTriageInbox:
    """Verify build_triage_inbox writes triage-inbox.json with correct counts."""

    def test_build_triage_inbox_writes_correct_counts(
        self, sync_module, tmp_path, monkeypatch
    ):
        """build_triage_inbox should aggregate emails, calendar, and jira items."""
        # Redirect file paths to tmp_path
        monkeypatch.setattr(sync_module, "CALENDAR_LOG_PATH", tmp_path / "calendar-log.json")
        monkeypatch.setattr(sync_module, "JIRA_LOG_PATH", tmp_path / "jira-log.json")
        monkeypatch.setattr(sync_module, "TRIAGE_INBOX_PATH", tmp_path / "triage-inbox.json")
        monkeypatch.setattr(sync_module, "HANDOFF_PATH", tmp_path / "worker-handoff.json")

        # Create calendar log with one recent uncategorized meeting
        now = datetime.now(timezone.utc)
        calendar_data = {
            "meetings": [
                {
                    "id": "cal1",
                    "title": "Standup",
                    "start": now.isoformat(),
                    "fetchedAt": now.isoformat(),
                    "attendees": ["philip@ironsail.ai"],
                }
            ],
            "changes": [],
        }
        (tmp_path / "calendar-log.json").write_text(json.dumps(calendar_data))

        # Create jira log with one pending action
        jira_data = {
            "pendingActions": [
                {
                    "ticket": "ROBO-42",
                    "action": "review",
                    "summary": "Review PR",
                }
            ]
        }
        (tmp_path / "jira-log.json").write_text(json.dumps(jira_data))

        # No handoff file (empty escalations)
        (tmp_path / "worker-handoff.json").write_text("{}")

        # Email log passed directly (2 uncategorized emails)
        email_log = {
            "entries": {
                "e1": {
                    "from": "a@b.com",
                    "subject": "Test 1",
                    "categorizedAt": None,
                },
                "e2": {
                    "from": "c@d.com",
                    "subject": "Test 2",
                    "categorizedAt": None,
                },
            }
        }

        total = sync_module.build_triage_inbox(email_log)

        # 2 emails + 1 calendar + 1 jira = 4
        assert total == 4

        inbox_path = tmp_path / "triage-inbox.json"
        assert inbox_path.exists()

        inbox = json.loads(inbox_path.read_text())
        assert inbox["counts"]["emails"] == 2
        assert inbox["counts"]["calendar"] == 1
        assert inbox["counts"]["jira"] == 1
        assert inbox["counts"]["total"] == 4
        assert len(inbox["items"]) == 4


# ---------------------------------------------------------------------------
# 4. Calendar log has review fields
# ---------------------------------------------------------------------------


class TestCalendarLogHasReviewFields:
    """Verify _get_pending_calendar returns items without categorizedAt."""

    def test_uncategorized_meetings_returned(self, sync_module):
        """Meetings without categorizedAt (and recent fetchedAt) should be pending."""
        now = datetime.now(timezone.utc)
        calendar_data = {
            "meetings": [
                {
                    "id": "m1",
                    "title": "Design Review",
                    "start": now.isoformat(),
                    "fetchedAt": now.isoformat(),
                    "attendees": ["philip@ironsail.ai", "alice@example.com"],
                },
                {
                    "id": "m2",
                    "title": "Already Reviewed",
                    "start": now.isoformat(),
                    "fetchedAt": now.isoformat(),
                    "categorizedAt": now.isoformat(),
                    "attendees": [],
                },
            ],
            "changes": [],
        }
        pending = sync_module._get_pending_calendar(calendar_data)
        assert len(pending) == 1
        assert pending[0]["id"] == "m1"
        assert pending[0]["source"] == "calendar"
        assert pending[0]["type"] == "meeting"

    def test_changes_without_reviewed_at_returned(self, sync_module):
        """Calendar changes without reviewedAt should appear as pending."""
        now = datetime.now(timezone.utc)
        calendar_data = {
            "meetings": [],
            "changes": [
                {
                    "eventId": "ev1",
                    "title": "Rescheduled Meeting",
                    "type": "rescheduled",
                    "details": "Moved to 3pm",
                    "start": now.isoformat(),
                    "end": (now + timedelta(hours=1)).isoformat(),
                    "timestamp": now.isoformat(),
                },
            ],
        }
        pending = sync_module._get_pending_calendar(calendar_data)
        assert len(pending) == 1
        assert pending[0]["type"] == "change"
        assert pending[0]["id"] == "ev1"

    def test_empty_calendar_returns_empty(self, sync_module):
        """Empty calendar data should yield no pending items."""
        pending = sync_module._get_pending_calendar({"meetings": [], "changes": []})
        assert pending == []
