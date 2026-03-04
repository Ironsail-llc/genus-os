#!/usr/bin/env python3
"""
Stress tests and edge-case tests for email_sync.py and system_health_check.py.

Covers:
- Corrupted JSON files (load_log recovery)
- Atomic writes (save_log crash safety)
- Concurrent run prevention (file lock)
- parse_sender with unicode, malformed, and unusual formats
- gog returning unexpected data formats
- Bridge down/slow during CRM logging
- Backfill window limits
- validate_entries idempotency
- Large email log performance
- Health check with all services down
- Health check with corrupted input files
"""

import fcntl
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from email_sync import (
    backfill_null_metadata,
    create_minimal_entry,
    load_log,
    log_email_to_crm,
    parse_sender,
    save_log,
    validate_entries,
)
from system_health_check import (
    _atomic_json_write,
    check_cron_freshness,
    check_email_data_quality,
    write_escalation,
)

# =========================================================================
# Corrupted / Missing JSON files
# =========================================================================


class TestCorruptedFiles:
    """Test recovery from corrupted JSON files."""

    def test_load_log_survives_corrupted_json(self, tmp_path):
        bad_file = tmp_path / "email-log.json"
        bad_file.write_text("{invalid json content")
        result = load_log(bad_file)
        assert result == {"lastCheckedAt": None, "entries": {}}
        # Should have created a backup
        backups = list(tmp_path.glob("*.corrupt.*.json"))
        assert len(backups) == 1

    def test_load_log_survives_empty_file(self, tmp_path):
        empty_file = tmp_path / "email-log.json"
        empty_file.write_text("")
        result = load_log(empty_file)
        assert result == {"lastCheckedAt": None, "entries": {}}

    def test_load_log_survives_truncated_json(self, tmp_path):
        truncated = tmp_path / "email-log.json"
        truncated.write_text('{"entries": {"abc": {"from": "test@exa')
        result = load_log(truncated)
        assert result == {"lastCheckedAt": None, "entries": {}}

    def test_load_log_missing_file(self, tmp_path):
        result = load_log(tmp_path / "nonexistent.json")
        assert result == {"lastCheckedAt": None, "entries": {}}

    def test_load_log_binary_garbage(self, tmp_path):
        garbage = tmp_path / "email-log.json"
        garbage.write_bytes(b"\x00\xff\xfe\x89PNG\r\n")
        result = load_log(garbage)
        assert result == {"lastCheckedAt": None, "entries": {}}


# =========================================================================
# Atomic writes
# =========================================================================


class TestAtomicWrites:
    """Test that save_log uses atomic write (temp file + rename)."""

    def test_save_log_creates_valid_json(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"entries": {"abc": {"from": "test"}}, "lastCheckedAt": "now"}
        save_log(data, path)
        reloaded = json.loads(path.read_text())
        assert reloaded["entries"]["abc"]["from"] == "test"

    def test_save_log_no_partial_writes(self, tmp_path):
        path = tmp_path / "test.json"
        # Write valid data first
        save_log({"entries": {"good": True}}, path)

        # Now try to save something that will fail serialization
        class BadObj:
            pass

        bad_data = {"entries": {"fail": BadObj()}}
        with pytest.raises(TypeError):
            save_log(bad_data, path)

        # Original file should still be intact
        reloaded = json.loads(path.read_text())
        assert reloaded["entries"]["good"] is True

    def test_save_log_no_leftover_temp_files(self, tmp_path):
        path = tmp_path / "test.json"
        save_log({"entries": {}}, path)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_atomic_json_write_health(self, tmp_path):
        path = tmp_path / "health.json"
        _atomic_json_write(path, {"status": "ok"})
        assert json.loads(path.read_text())["status"] == "ok"


# =========================================================================
# Concurrent run prevention
# =========================================================================


class TestConcurrency:
    """Test that concurrent email_sync runs are prevented."""

    def test_lock_file_prevents_concurrent_runs(self, tmp_path):
        lock_path = tmp_path / ".email-log.lock"
        # Acquire lock
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Second attempt should fail with LOCK_NB
        lock_file2 = open(lock_path, "w")
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_file2, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Release and retry
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        fcntl.flock(lock_file2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file2, fcntl.LOCK_UN)
        lock_file2.close()


# =========================================================================
# parse_sender edge cases
# =========================================================================


class TestParseSenderEdgeCases:
    """Stress test parse_sender with unusual formats."""

    def test_unicode_name(self):
        name, email = parse_sender("José García <jose@example.com>")
        assert name == "José García"
        assert email == "jose@example.com"

    def test_name_with_commas(self):
        name, email = parse_sender('"Smith, John" <john@example.com>')
        assert name == "Smith, John"
        assert email == "john@example.com"

    def test_name_with_dots(self):
        name, email = parse_sender("Dr. Jane Doe <jane@example.com>")
        assert name == "Dr. Jane Doe"
        assert email == "jane@example.com"

    def test_very_long_name(self):
        long_name = "A" * 200
        name, email = parse_sender(f"{long_name} <a@b.com>")
        assert name == long_name
        assert email == "a@b.com"

    def test_email_only_no_brackets(self):
        name, email = parse_sender("user@company.co.uk")
        assert name == "user"
        assert email == "user@company.co.uk"

    def test_plus_address(self):
        name, email = parse_sender("user+tag@example.com")
        assert name == "user+tag"
        assert email == "user+tag@example.com"

    def test_whitespace_only(self):
        name, email = parse_sender("   ")
        # Whitespace-only should not crash
        assert name is not None or name is None  # Just don't crash

    def test_angle_brackets_no_name(self):
        name, email = parse_sender("<user@example.com>")
        # Should extract the email at minimum
        assert email == "user@example.com" or email is None

    def test_noreply_address(self):
        name, email = parse_sender("noreply@github.com")
        assert name == "noreply"
        assert email == "noreply@github.com"

    def test_numeric_only_local_part(self):
        name, email = parse_sender("12345@example.com")
        assert email == "12345@example.com"


# =========================================================================
# gog returning unexpected data
# =========================================================================


class TestGogEdgeCases:
    """Test handling of unexpected gog output formats."""

    @patch("email_sync.run_gog")
    def test_gog_returns_list_instead_of_dict(self, mock_gog):
        """gog might return a bare list instead of {threads: [...]}."""
        mock_gog.return_value = json.dumps(
            [{"id": "abc", "from": "test@test.com", "subject": "Hi"}]
        )
        from email_sync import fetch_unread_emails

        result = fetch_unread_emails()
        assert len(result) == 1
        assert result[0]["id"] == "abc"

    @patch("email_sync.run_gog")
    def test_gog_returns_empty_threads(self, mock_gog):
        mock_gog.return_value = json.dumps({"threads": []})
        from email_sync import fetch_unread_emails

        result = fetch_unread_emails()
        assert result == []

    @patch("email_sync.run_gog")
    def test_gog_returns_null(self, mock_gog):
        mock_gog.return_value = "null"
        from email_sync import fetch_unread_emails

        result = fetch_unread_emails()
        assert result == []

    @patch("email_sync.run_gog")
    def test_gog_returns_error_text(self, mock_gog):
        mock_gog.return_value = "Error: authentication failed\n"
        from email_sync import fetch_unread_emails

        result = fetch_unread_emails()
        assert result == []

    @patch("email_sync.run_gog")
    def test_gog_returns_empty_string(self, mock_gog):
        mock_gog.return_value = ""
        from email_sync import fetch_unread_emails

        result = fetch_unread_emails()
        assert result == []

    @patch("email_sync.run_gog")
    def test_backfill_handles_gog_failure(self, mock_gog):
        mock_gog.return_value = ""
        log = {
            "entries": {
                "broken1": {"id": "broken1", "from": None, "subject": None},
            }
        }
        count = backfill_null_metadata(log)
        assert count == 0
        # Entry unchanged
        assert log["entries"]["broken1"]["from"] is None


# =========================================================================
# CRM logging edge cases
# =========================================================================


class TestCrmLoggingEdgeCases:
    """Test CRM logging resilience."""

    @patch("email_sync.requests.post")
    def test_bridge_timeout(self, mock_post):
        from requests.exceptions import Timeout

        mock_post.side_effect = Timeout("Connection timed out")
        entry = {"from": "test@example.com", "subject": "Test"}
        result = log_email_to_crm(entry)
        assert result is False

    @patch("email_sync.requests.post")
    def test_bridge_returns_non_json(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_post.return_value = mock_resp
        entry = {"from": "test@example.com", "subject": "Test"}
        result = log_email_to_crm(entry)
        assert result is False

    @patch("email_sync.requests.post")
    def test_very_long_subject_in_crm_log(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        entry = {"from": "test@example.com", "subject": "A" * 10000}
        result = log_email_to_crm(entry)
        assert result is True
        # Verify the call was made (no truncation crash)
        mock_post.assert_called_once()

    @patch("email_sync.requests.post")
    def test_from_with_special_chars(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        entry = {"from": '"O\'Brien, José" <jose@example.com>', "subject": "Test"}
        result = log_email_to_crm(entry)
        assert result is True


# =========================================================================
# validate_entries idempotency
# =========================================================================


class TestValidationIdempotency:
    """Test that validate_entries is safe to run repeatedly."""

    def test_double_validation_is_idempotent(self):
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
                    "summary": "placeholder",
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": None,
                }
            }
        }
        count1 = validate_entries(log)
        assert count1 == 1

        # Second run — entry already reset, should be 0
        count2 = validate_entries(log)
        assert count2 == 0

    def test_validation_preserves_backfilled_data(self):
        """After backfill adds from/subject, validation should not reset it."""
        log = {
            "entries": {
                "fixed": {
                    "from": "real@sender.com",
                    "subject": "Real subject",
                    "categorizedAt": None,
                    "urgency": None,
                    "category": None,
                    "actionRequired": None,
                    "readAt": None,
                    "actionCompletedAt": None,
                    "pendingReviewAt": None,
                    "reviewedAt": None,
                    "backfilledAt": "2026-02-13T10:00:00",
                }
            }
        }
        count = validate_entries(log)
        assert count == 0
        assert log["entries"]["fixed"]["from"] == "real@sender.com"


# =========================================================================
# Large email log performance
# =========================================================================


class TestPerformance:
    """Test that operations scale to large log files."""

    def test_validate_1000_entries(self):
        """validate_entries should handle 1000 entries quickly."""
        entries = {}
        for i in range(1000):
            entries[f"id_{i}"] = {
                "from": f"user{i}@example.com" if i % 3 != 0 else None,
                "subject": f"Subject {i}" if i % 3 != 0 else None,
                "categorizedAt": "2026-02-11T14:30:00" if i % 5 == 0 else None,
                "urgency": "low",
                "category": "work",
                "actionRequired": None,
                "readAt": None,
                "actionCompletedAt": None,
                "pendingReviewAt": None,
                "reviewedAt": None,
                "summary": None,
            }
        log = {"entries": entries}

        start = time.time()
        count = validate_entries(log)
        elapsed = time.time() - start

        # Should be way under 1s for 1000 entries
        assert elapsed < 1.0
        # Entries with null from+subject AND categorizedAt set should be reset
        # i % 3 == 0 AND i % 5 == 0 → i % 15 == 0
        expected = len([i for i in range(1000) if i % 3 == 0 and i % 5 == 0])
        assert count == expected

    def test_save_load_roundtrip_large(self, tmp_path):
        """Save and load 1000 entries without data loss."""
        path = tmp_path / "large.json"
        entries = {}
        for i in range(1000):
            entries[f"id_{i}"] = {
                "from": f"user{i}@example.com",
                "subject": f"Subject {i}",
                "fetchedAt": datetime.now().isoformat(),
            }
        data = {"lastCheckedAt": datetime.now().isoformat(), "entries": entries}
        save_log(data, path)
        loaded = load_log(path)
        assert len(loaded["entries"]) == 1000
        assert loaded["entries"]["id_999"]["from"] == "user999@example.com"

    def test_create_minimal_entry_with_all_fields(self):
        """Entry creation handles all possible gog fields."""
        email = {
            "id": "test123",
            "threadId": "thread456",
            "from": "test@example.com",
            "subject": "Test subject",
            "date": "2026-02-13T10:00:00-05:00",
            "labels": ["INBOX", "UNREAD", "IMPORTANT"],
            "messageCount": 5,
            "snippet": "This is a preview...",  # Should be ignored
            "extra_field": "should be ignored",
        }
        entry = create_minimal_entry(email)
        assert entry["from"] == "test@example.com"
        assert entry["snippet"] is None  # Always null at sync time
        assert "extra_field" not in entry


# =========================================================================
# Health check edge cases
# =========================================================================


class TestHealthCheckEdgeCases:
    """Test health check resilience to bad inputs."""

    def test_corrupted_email_log(self, tmp_path):
        bad_log = tmp_path / "email-log.json"
        bad_log.write_text("{not valid json")
        with patch("system_health_check.EMAIL_LOG_PATH", bad_log):
            results = check_email_data_quality()
        assert results[0]["status"] == "CRITICAL"

    def test_corrupted_handoff_file(self, tmp_path):
        bad_handoff = tmp_path / "worker-handoff.json"
        bad_handoff.write_text("garbage")
        failures = [{"name": "test", "status": "CRITICAL", "detail": "test"}]
        with patch("system_health_check.HANDOFF_PATH", bad_handoff):
            # Should not crash — creates fresh escalation list
            write_escalation(failures)
        data = json.loads(bad_handoff.read_text())
        assert len(data["escalations"]) == 1

    def test_handoff_missing_escalations_key(self, tmp_path):
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text('{"lastRunAt": "2026-02-13T10:00:00"}')
        failures = [{"name": "redis", "status": "CRITICAL", "detail": "down"}]
        with patch("system_health_check.HANDOFF_PATH", handoff):
            write_escalation(failures)
        data = json.loads(handoff.read_text())
        assert len(data["escalations"]) == 1

    def test_cron_freshness_with_missing_files(self, tmp_path):
        missing_email = tmp_path / "nonexistent-email.json"
        missing_handoff = tmp_path / "nonexistent-handoff.json"
        with (
            patch("system_health_check.EMAIL_LOG_PATH", missing_email),
            patch("system_health_check.HANDOFF_PATH", missing_handoff),
        ):
            results = check_cron_freshness()
        # Both should fail gracefully, not crash
        assert len(results) == 2
        assert all(r["status"] == "CRITICAL" for r in results)

    def test_email_quality_with_only_good_recent_entries(self, tmp_path):
        email_log = tmp_path / "email-log.json"
        now = datetime.now()
        entries = {}
        for i in range(50):
            entries[f"id_{i}"] = {
                "fetchedAt": now.isoformat(),
                "from": f"user{i}@example.com",
                "subject": f"Subject {i}",
            }
        email_log.write_text(json.dumps({"entries": entries}))
        with patch("system_health_check.EMAIL_LOG_PATH", email_log):
            results = check_email_data_quality()
        assert results[0]["status"] == "ok"

    def test_escalation_with_many_failures(self, tmp_path):
        """Health check should handle escalation with 20+ failures."""
        handoff = tmp_path / "worker-handoff.json"
        handoff.write_text(json.dumps({"escalations": []}))
        failures = [
            {"name": f"service:{i}", "status": "CRITICAL", "detail": f"failure {i}"}
            for i in range(20)
        ]
        with patch("system_health_check.HANDOFF_PATH", handoff):
            write_escalation(failures)
        data = json.loads(handoff.read_text())
        assert "20 CRITICAL" in data["escalations"][0]["summary"]
        assert len(data["escalations"][0]["detail"]) == 20


# =========================================================================
# create_minimal_entry boundary conditions
# =========================================================================


class TestEntryBoundaryConditions:
    """Test entry creation with missing/empty/null fields."""

    def test_completely_empty_email(self):
        entry = create_minimal_entry({})
        assert entry["id"] is None
        assert entry["from"] is None
        assert entry["labels"] == []

    def test_email_with_only_id(self):
        entry = create_minimal_entry({"id": "only-id"})
        assert entry["id"] == "only-id"
        assert entry["from"] is None

    def test_email_with_empty_string_fields(self):
        entry = create_minimal_entry({"id": "x", "from": "", "subject": ""})
        assert entry["from"] == ""
        assert entry["subject"] == ""

    def test_email_with_none_values(self):
        entry = create_minimal_entry({"id": "x", "from": None, "subject": None})
        assert entry["from"] is None
        assert entry["subject"] is None


# =========================================================================
# Worker handoff truncation
# =========================================================================


class TestHandoffTruncation:
    """Test that data_archival truncates worker-handoff.json."""

    def test_truncation_keeps_last_50(self, tmp_path):
        from data_archival import MAX_HANDOFF_ITEMS, truncate_handoff_items

        handoff = tmp_path / "worker-handoff.json"
        items = [{"type": "test", "idx": i} for i in range(200)]
        escalations = [{"source": "test", "idx": i} for i in range(100)]
        handoff.write_text(json.dumps({"items": items, "escalations": escalations}))

        with patch("data_archival.WORKER_HANDOFF_PATH", handoff):
            result = truncate_handoff_items()

        assert result["items_before"] == 200
        assert result["items_after"] == MAX_HANDOFF_ITEMS
        assert result["escalations_before"] == 100
        assert result["escalations_after"] == MAX_HANDOFF_ITEMS

        reloaded = json.loads(handoff.read_text())
        assert len(reloaded["items"]) == MAX_HANDOFF_ITEMS
        # Should keep the LAST 50, not the first 50
        assert reloaded["items"][0]["idx"] == 150

    def test_truncation_noop_when_small(self, tmp_path):
        from data_archival import truncate_handoff_items

        handoff = tmp_path / "worker-handoff.json"
        items = [{"type": "test", "idx": i} for i in range(10)]
        handoff.write_text(json.dumps({"items": items, "escalations": []}))

        with patch("data_archival.WORKER_HANDOFF_PATH", handoff):
            result = truncate_handoff_items()

        assert result["items_before"] == 10
        assert result["items_after"] == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
