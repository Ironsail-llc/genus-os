#!/usr/bin/env python3
"""
Tests for email_sync.py fixes: metadata preservation, validation guard,
backfill, and CRM logging.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from email_sync import (
    REPLY_COOLDOWN_SECONDS,
    backfill_null_metadata,
    create_minimal_entry,
    log_email_to_crm,
    parse_sender,
    should_skip_reset,
    validate_entries,
)


class TestCreateMinimalEntry:
    """Test that create_minimal_entry preserves metadata from gog search results."""

    def test_preserves_from_field(self):
        email = {"id": "abc123", "from": "Philip <philip@ironsail.ai>", "subject": "Test"}
        entry = create_minimal_entry(email)
        assert entry["from"] == "Philip <philip@ironsail.ai>"

    def test_preserves_subject_field(self):
        email = {"id": "abc123", "from": "a@b.com", "subject": "Important meeting"}
        entry = create_minimal_entry(email)
        assert entry["subject"] == "Important meeting"

    def test_preserves_date_and_labels(self):
        email = {
            "id": "abc123",
            "date": "2026-02-13T10:00:00-05:00",
            "labels": ["INBOX", "UNREAD"],
        }
        entry = create_minimal_entry(email)
        assert entry["date"] == "2026-02-13T10:00:00-05:00"
        assert entry["labels"] == ["INBOX", "UNREAD"]

    def test_snippet_still_null(self):
        email = {"id": "abc123", "from": "a@b.com", "subject": "Test"}
        entry = create_minimal_entry(email)
        assert entry["snippet"] is None

    def test_notifier_fields_start_null(self):
        email = {"id": "abc123", "from": "a@b.com", "subject": "Test"}
        entry = create_minimal_entry(email)
        assert entry["categorizedAt"] is None
        assert entry["urgency"] is None
        assert entry["category"] is None
        assert entry["actionRequired"] is None
        assert entry["readAt"] is None

    def test_missing_from_stays_none(self):
        email = {"id": "abc123"}
        entry = create_minimal_entry(email)
        assert entry["from"] is None
        assert entry["subject"] is None

    def test_labels_default_to_empty_list(self):
        email = {"id": "abc123"}
        entry = create_minimal_entry(email)
        assert entry["labels"] == []


class TestValidateEntries:
    """Test the content validation guard."""

    def test_resets_categorized_null_entry(self):
        log = {
            "entries": {
                "bad1": {
                    "from": None,
                    "subject": None,
                    "categorizedAt": "2026-02-11T14:30:00",
                    "urgency": "low",
                    "category": "notification",
                    "actionRequired": "none",
                    "readAt": "2026-02-11T14:30:00",
                    "summary": "Empty system placeholder",
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": "2026-02-11T14:30:00",
                }
            }
        }
        count = validate_entries(log)
        assert count == 1
        entry = log["entries"]["bad1"]
        assert entry["categorizedAt"] is None
        assert entry["urgency"] is None
        assert entry["readAt"] is None
        assert entry["summary"] is None
        assert entry["resetByValidation"] is True

    def test_does_not_reset_good_entry(self):
        log = {
            "entries": {
                "good1": {
                    "from": "philip@ironsail.ai",
                    "subject": "Test email",
                    "categorizedAt": "2026-02-11T14:30:00",
                    "urgency": "medium",
                    "category": "work",
                    "actionRequired": "review",
                    "readAt": "2026-02-11T14:30:00",
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": None,
                }
            }
        }
        count = validate_entries(log)
        assert count == 0
        assert log["entries"]["good1"]["categorizedAt"] == "2026-02-11T14:30:00"

    def test_does_not_reset_uncategorized_null_entry(self):
        log = {
            "entries": {
                "new1": {
                    "from": None,
                    "subject": None,
                    "categorizedAt": None,
                    "urgency": None,
                    "category": None,
                    "actionRequired": None,
                    "readAt": None,
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": None,
                }
            }
        }
        count = validate_entries(log)
        assert count == 0

    def test_does_not_reset_entry_with_from_only(self):
        log = {
            "entries": {
                "partial": {
                    "from": "someone@example.com",
                    "subject": None,
                    "categorizedAt": "2026-02-11T14:30:00",
                    "urgency": "low",
                    "category": "notification",
                    "actionRequired": None,
                    "readAt": None,
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": None,
                }
            }
        }
        count = validate_entries(log)
        assert count == 0


class TestParseSender:
    """Test sender name/email parsing."""

    def test_name_angle_bracket_format(self):
        name, email = parse_sender("Philip Deng <philip@ironsail.ai>")
        assert name == "Philip Deng"
        assert email == "philip@ironsail.ai"

    def test_quoted_name_format(self):
        name, email = parse_sender('"Philip Deng" <philip@ironsail.ai>')
        assert name == "Philip Deng"
        assert email == "philip@ironsail.ai"

    def test_bare_email(self):
        name, email = parse_sender("noreply@github.com")
        assert name == "noreply"
        assert email == "noreply@github.com"

    def test_none_input(self):
        name, email = parse_sender(None)
        assert name is None
        assert email is None

    def test_empty_string(self):
        name, email = parse_sender("")
        assert name is None
        assert email is None


class TestLogEmailToCrm:
    """Test CRM logging via Bridge."""

    @patch("email_sync.requests.post")
    def test_logs_real_email_to_bridge(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        entry = {
            "from": "Philip Deng <philip@ironsail.ai>",
            "subject": "Test email",
        }
        result = log_email_to_crm(entry)
        assert result is True
        mock_post.assert_called_once()
        call_json = mock_post.call_args[1]["json"]
        assert call_json["contact_name"] == "Philip Deng"
        assert call_json["channel"] == "email"
        assert call_json["direction"] == "incoming"
        assert call_json["channel_identifier"] == "philip@ironsail.ai"

    @patch("email_sync.requests.post")
    def test_skips_null_sender(self, mock_post):
        entry = {"from": None, "subject": "Test"}
        result = log_email_to_crm(entry)
        assert result is False
        mock_post.assert_not_called()

    @patch("email_sync.requests.post")
    def test_handles_bridge_failure(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)
        entry = {"from": "test@example.com", "subject": "Test"}
        result = log_email_to_crm(entry)
        assert result is False

    @patch("email_sync.requests.post")
    def test_handles_connection_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")
        entry = {"from": "test@example.com", "subject": "Test"}
        result = log_email_to_crm(entry)
        assert result is False


class TestBackfillNullMetadata:
    """Test backfill of broken entries."""

    @patch("email_sync.fetch_message_metadata")
    def test_backfills_matching_entries(self, mock_fetch):
        mock_fetch.return_value = {
            "from": "real@sender.com",
            "subject": "Real subject",
            "date": "2026-02-13",
            "labels": ["INBOX"],
            "threadId": "broken1",
        }
        log = {
            "entries": {
                "broken1": {
                    "id": "broken1",
                    "from": None,
                    "subject": None,
                    "categorizedAt": "2026-02-11T14:30:00",
                    "urgency": "low",
                    "category": "notification",
                    "actionRequired": "none",
                    "readAt": "2026-02-11T14:30:00",
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": None,
                }
            }
        }
        count = backfill_null_metadata(log)
        assert count == 1
        entry = log["entries"]["broken1"]
        assert entry["from"] == "real@sender.com"
        assert entry["subject"] == "Real subject"
        assert entry["categorizedAt"] is None  # Reset for re-processing
        assert entry["backfilledVia"] == "gws-api"
        mock_fetch.assert_called_once_with("broken1")

    @patch("email_sync.fetch_message_metadata")
    def test_no_op_when_all_entries_have_metadata(self, mock_fetch):
        log = {
            "entries": {
                "good1": {"id": "good1", "from": "a@b.com", "subject": "Hi"},
            }
        }
        count = backfill_null_metadata(log)
        assert count == 0
        mock_fetch.assert_not_called()


class TestShouldSkipReset:
    """Test the reply cooldown logic."""

    def test_skip_within_cooldown(self):
        from datetime import timedelta

        now = datetime.now(UTC)
        recent = (now - timedelta(seconds=60)).isoformat()
        entry = {"actionCompletedAt": recent}
        assert should_skip_reset(entry) is True

    def test_allow_after_cooldown(self):
        from datetime import timedelta

        now = datetime.now(UTC)
        old = (now - timedelta(seconds=REPLY_COOLDOWN_SECONDS + 60)).isoformat()
        entry = {"actionCompletedAt": old}
        assert should_skip_reset(entry) is False

    def test_allow_no_action_completed(self):
        entry = {"actionCompletedAt": None}
        assert should_skip_reset(entry) is False

    def test_allow_missing_field(self):
        entry = {}
        assert should_skip_reset(entry) is False

    def test_handles_naive_timestamp(self):
        from datetime import timedelta

        now = datetime.now(UTC)
        # Naive timestamp (no timezone) within cooldown
        recent = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")
        entry = {"actionCompletedAt": recent}
        assert should_skip_reset(entry) is True

    def test_handles_invalid_timestamp(self):
        entry = {"actionCompletedAt": "not-a-date"}
        assert should_skip_reset(entry) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
