#!/usr/bin/env python3
"""
Tests for Calendar Sync.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from calendar_sync import (
    detect_changes,
    extract_conference_url,
    normalize_event,
)


class TestNormalizeEvent:
    """Test event normalization."""

    def test_normalize_google_event(self):
        event = {
            "id": "abc123",
            "summary": "Team Meeting",
            "start": {"dateTime": "2026-02-05T10:00:00-05:00"},
            "end": {"dateTime": "2026-02-05T11:00:00-05:00"},
            "location": "Conference Room A",
            "attendees": [{"email": "philip@ironsail.ai"}, {"email": "jon@digitalrx.com"}],
        }

        normalized = normalize_event(event)

        assert normalized["id"] == "abc123"
        assert normalized["title"] == "Team Meeting"
        assert normalized["start"] == "2026-02-05T10:00:00-05:00"
        assert normalized["location"] == "Conference Room A"
        assert len(normalized["attendees"]) == 2
        assert normalized["notifiedAt"] is None

    def test_normalize_all_day_event(self):
        event = {
            "id": "def456",
            "summary": "Holiday",
            "start": {"date": "2026-02-14"},
            "end": {"date": "2026-02-15"},
        }

        normalized = normalize_event(event)

        assert normalized["start"] == "2026-02-14"
        assert normalized["end"] == "2026-02-15"

    def test_normalize_missing_summary(self):
        event = {
            "id": "xyz",
            "start": {"dateTime": "2026-02-05T10:00:00-05:00"},
            "end": {"dateTime": "2026-02-05T11:00:00-05:00"},
        }

        normalized = normalize_event(event)

        assert normalized["title"] == "Untitled"


class TestExtractConferenceUrl:
    """Test conference URL extraction."""

    def test_extract_from_conference_data(self):
        event = {
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                ]
            }
        }

        url = extract_conference_url(event)
        assert url == "https://meet.google.com/abc-defg-hij"

    def test_extract_hangout_link(self):
        event = {"hangoutLink": "https://meet.google.com/xyz-123"}

        url = extract_conference_url(event)
        assert url == "https://meet.google.com/xyz-123"

    def test_extract_zoom_from_description(self):
        event = {"description": "Join the meeting at https://zoom.us/j/123456789 for discussion"}

        url = extract_conference_url(event)
        assert url == "https://zoom.us/j/123456789"

    def test_no_conference_url(self):
        event = {"summary": "In-person meeting", "location": "Office"}

        url = extract_conference_url(event)
        assert url is None


class TestDetectChanges:
    """Test change detection."""

    def test_detect_new_event(self):
        new_events = [
            {
                "id": "new1",
                "summary": "New Meeting",
                "start": {"dateTime": "2026-02-10T10:00:00-05:00"},
                "end": {"dateTime": "2026-02-10T11:00:00-05:00"},
            }
        ]
        existing = []

        updated, changes, cancelled = detect_changes(new_events, existing)

        assert len(updated) == 1
        assert len(changes) == 1
        assert changes[0]["type"] == "new"
        assert changes[0]["eventId"] == "new1"
        assert changes[0]["reviewedAt"] is None

    def test_detect_updated_event(self):
        new_events = [
            {
                "id": "event1",
                "summary": "Updated Title",
                "start": {"dateTime": "2026-02-10T10:00:00-05:00"},
                "end": {"dateTime": "2026-02-10T11:00:00-05:00"},
            }
        ]
        existing = [
            {
                "id": "event1",
                "title": "Original Title",
                "start": "2026-02-10T10:00:00-05:00",
                "end": "2026-02-10T11:00:00-05:00",
                "notifiedAt": "2026-02-05T09:00:00-05:00",
            }
        ]

        updated, changes, cancelled = detect_changes(new_events, existing)

        assert len(changes) == 1
        assert changes[0]["type"] == "updated"
        assert "title" in changes[0]["summary"]
        # Should preserve notifiedAt
        assert updated[0]["notifiedAt"] == "2026-02-05T09:00:00-05:00"

    def test_detect_cancelled_event(self):
        now = datetime.now()
        future_time = (now + timedelta(days=5)).isoformat()

        new_events = []
        existing = [
            {
                "id": "cancelled1",
                "title": "Meeting That Got Cancelled",
                "start": future_time,
                "end": future_time,
            }
        ]

        updated, changes, cancelled = detect_changes(new_events, existing)

        assert len(cancelled) == 1
        assert len(changes) == 1
        assert changes[0]["type"] == "cancelled"
        assert changes[0]["eventId"] == "cancelled1"

    def test_no_change_preserves_event(self):
        new_events = [
            {
                "id": "event1",
                "summary": "Same Meeting",
                "start": {"dateTime": "2026-02-10T10:00:00-05:00"},
                "end": {"dateTime": "2026-02-10T11:00:00-05:00"},
            }
        ]
        existing = [
            {
                "id": "event1",
                "title": "Same Meeting",
                "start": "2026-02-10T10:00:00-05:00",
                "end": "2026-02-10T11:00:00-05:00",
                "notifiedAt": "2026-02-05T09:00:00-05:00",
            }
        ]

        updated, changes, cancelled = detect_changes(new_events, existing)

        assert len(updated) == 1
        assert len(changes) == 0  # No changes
        assert updated[0]["notifiedAt"] == "2026-02-05T09:00:00-05:00"

    def test_time_change_detected(self):
        new_events = [
            {
                "id": "event1",
                "summary": "Meeting",
                "start": {"dateTime": "2026-02-10T14:00:00-05:00"},  # Changed from 10:00 to 14:00
                "end": {"dateTime": "2026-02-10T15:00:00-05:00"},
            }
        ]
        existing = [
            {
                "id": "event1",
                "title": "Meeting",
                "start": "2026-02-10T10:00:00-05:00",
                "end": "2026-02-10T11:00:00-05:00",
            }
        ]

        updated, changes, cancelled = detect_changes(new_events, existing)

        assert len(changes) == 1
        assert changes[0]["type"] == "updated"
        assert "start time" in changes[0]["summary"]


class TestIntegration:
    """Integration tests with mocked gog CLI."""

    @pytest.fixture
    def mock_gog_output(self):
        return json.dumps(
            [
                {
                    "id": "event1",
                    "summary": "Daily Standup",
                    "start": {"dateTime": "2026-02-05T09:00:00-05:00"},
                    "end": {"dateTime": "2026-02-05T09:30:00-05:00"},
                }
            ]
        )

    def test_sync_creates_log(self, tmp_path, mock_gog_output):
        log_file = tmp_path / "calendar-log.json"

        with (
            patch("calendar_sync.CALENDAR_LOG", log_file),
            patch("calendar_sync.subprocess.run") as mock_run,
            patch("calendar_sync.CronContext.load") as mock_ctx,
        ):
            # Mock gog output
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_gog_output, stderr="")

            # Mock context
            mock_ctx.return_value = MagicMock(
                tasks=MagicMock(pending=[]), emails=MagicMock(unsurfaced=[])
            )

            from calendar_sync import sync

            sync()

        # Verify log was created
        assert log_file.exists()
        data = json.loads(log_file.read_text())
        assert len(data["meetings"]) == 1
        assert data["meetings"][0]["title"] == "Daily Standup"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
