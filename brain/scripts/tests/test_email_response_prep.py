#!/usr/bin/env python3
"""Tests for email_response_prep.py enrichment workers.

Tests the 4 new enrichment functions and depth classifier:
  - enrich_topic_rag() — RAG orchestrator search
  - enrich_calendar_context() — calendar log matching
  - enrich_crm_history() — CRM conversation history
  - classify_depth() — quick vs analytical classification
  - enrich_item() — full enrichment pipeline integration
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_crm_dal():
    """Mock crm_dal import so tests don't need DB."""
    mock_dal = MagicMock()
    mock_dal.get_person.return_value = None
    mock_dal.get_conversations_for_contact.return_value = []
    with patch.dict(sys.modules, {"crm_dal": mock_dal}):
        yield mock_dal


@pytest.fixture
def prep_module(_mock_crm_dal):
    """Import the module fresh for each test."""
    mod_name = "email_response_prep"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import email_response_prep

    return email_response_prep


# ---------------------------------------------------------------------------
# enrich_topic_rag
# ---------------------------------------------------------------------------


class TestEnrichTopicRag:
    def test_empty_inputs_returns_empty(self, prep_module):
        assert prep_module.enrich_topic_rag(None, None) == []
        assert prep_module.enrich_topic_rag("", "") == []

    def test_successful_search(self, prep_module):
        mock_httpx = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {"text": "Revenue was $50K in Q1", "score": 0.85},
                {"text": "Budget approved for Q2", "score": 0.72},
            ]
        }
        mock_httpx.post.return_value = mock_resp

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = prep_module.enrich_topic_rag(
                "Q1 Revenue Report", "Here is the revenue data..."
            )

        assert len(result) == 2
        assert result[0]["text"] == "Revenue was $50K in Q1"
        assert result[0]["score"] == 0.85

    def test_http_error_returns_empty(self, prep_module):
        mock_httpx = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_httpx.post.return_value = mock_resp

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = prep_module.enrich_topic_rag("test", "test content")
        assert result == []

    def test_timeout_returns_empty(self, prep_module):
        mock_httpx = MagicMock()
        mock_httpx.post.side_effect = Exception("Connection timeout")

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            result = prep_module.enrich_topic_rag("test", "test content")
        assert result == []

    def test_truncates_thread_text(self, prep_module):
        mock_httpx = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_httpx.post.return_value = mock_resp

        long_text = "x" * 10000

        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            prep_module.enrich_topic_rag("subject", long_text)

        call_args = mock_httpx.post.call_args
        query = call_args[1]["json"]["query"]
        # Subject + space + first 500 chars of thread
        assert len(query) <= len("subject") + 1 + 500


# ---------------------------------------------------------------------------
# enrich_calendar_context
# ---------------------------------------------------------------------------


class TestEnrichCalendarContext:
    def test_empty_inputs_returns_empty(self, prep_module):
        assert prep_module.enrich_calendar_context(None, None) == []

    def test_matches_by_email(self, prep_module, tmp_path):
        now = datetime.now(UTC)
        cal_log = {
            "entries": {
                "evt1": {
                    "title": "Weekly Standup",
                    "start": (now - timedelta(days=2)).isoformat(),
                    "attendees": ["alice@example.com", "bob@example.com"],
                },
                "evt2": {
                    "title": "Old Meeting",
                    "start": (now - timedelta(days=30)).isoformat(),
                    "attendees": ["alice@example.com"],
                },
            }
        }
        cal_path = tmp_path / "calendar-log.json"
        cal_path.write_text(json.dumps(cal_log))

        with patch.object(prep_module, "CALENDAR_LOG_PATH", str(cal_path)):
            result = prep_module.enrich_calendar_context("Alice", "alice@example.com")

        assert len(result) == 1  # Only evt1 is within 14 days
        assert result[0]["title"] == "Weekly Standup"

    def test_matches_by_name_in_attendees(self, prep_module, tmp_path):
        now = datetime.now(UTC)
        cal_log = {
            "entries": {
                "evt1": {
                    "title": "Project Review",
                    "start": (now + timedelta(days=3)).isoformat(),
                    "attendees": ["Bob Smith", "alice@example.com"],
                },
            }
        }
        cal_path = tmp_path / "calendar-log.json"
        cal_path.write_text(json.dumps(cal_log))

        with patch.object(prep_module, "CALENDAR_LOG_PATH", str(cal_path)):
            result = prep_module.enrich_calendar_context("Bob Smith", None)

        assert len(result) == 1
        assert result[0]["title"] == "Project Review"

    def test_max_5_results(self, prep_module, tmp_path):
        now = datetime.now(UTC)
        entries = {}
        for i in range(10):
            entries[f"evt{i}"] = {
                "title": f"Meeting {i}",
                "start": (now - timedelta(days=i)).isoformat(),
                "attendees": ["alice@example.com"],
            }
        cal_path = tmp_path / "calendar-log.json"
        cal_path.write_text(json.dumps({"entries": entries}))

        with patch.object(prep_module, "CALENDAR_LOG_PATH", str(cal_path)):
            result = prep_module.enrich_calendar_context("Alice", "alice@example.com")

        assert len(result) <= 5

    def test_missing_calendar_file(self, prep_module, tmp_path):
        with patch.object(prep_module, "CALENDAR_LOG_PATH", str(tmp_path / "nonexistent.json")):
            result = prep_module.enrich_calendar_context("Alice", "alice@example.com")
        assert result == []


# ---------------------------------------------------------------------------
# enrich_crm_history
# ---------------------------------------------------------------------------


class TestEnrichCrmHistory:
    def test_none_person_id_returns_empty(self, prep_module):
        assert prep_module.enrich_crm_history(None) == []

    def test_successful_fetch(self, prep_module, _mock_crm_dal):
        _mock_crm_dal.get_conversations_for_contact.return_value = [
            {"inboxName": "Email", "status": "open", "lastActivityAt": "2026-02-20T10:00:00Z"},
            {
                "inboxName": "Telegram",
                "status": "resolved",
                "lastActivityAt": "2026-02-19T15:00:00Z",
            },
        ]

        result = prep_module.enrich_crm_history("person-123")
        assert len(result) == 2
        assert result[0]["channel"] == "Email"
        assert result[0]["status"] == "open"

    def test_limits_results(self, prep_module, _mock_crm_dal):
        _mock_crm_dal.get_conversations_for_contact.return_value = [
            {
                "inboxName": f"Channel{i}",
                "status": "open",
                "lastActivityAt": f"2026-02-{20 - i:02d}T10:00:00Z",
            }
            for i in range(10)
        ]

        result = prep_module.enrich_crm_history("person-123", limit=3)
        assert len(result) == 3

    def test_db_error_returns_empty(self, prep_module, _mock_crm_dal):
        _mock_crm_dal.get_conversations_for_contact.side_effect = Exception("DB error")

        result = prep_module.enrich_crm_history("person-123")
        assert result == []


# ---------------------------------------------------------------------------
# classify_depth
# ---------------------------------------------------------------------------


class TestClassifyDepth:
    def test_analytical_classification_is_analytical(self, prep_module):
        item = {"classification": "analytical", "thread": "short"}
        assert prep_module.classify_depth(item) == "analytical"

    def test_two_signals_is_analytical(self, prep_module):
        item = {
            "classification": "info_received",
            "subject": "Q1 Revenue Report",
            "thread": "Revenue was $50,000. Budget increased 15% this quarter.",
        }
        assert prep_module.classify_depth(item) == "analytical"

    def test_dollar_and_signal_is_analytical(self, prep_module):
        item = {
            "classification": "info_received",
            "subject": "Cashflow Snapshot",
            "thread": "Total: $125,000",
        }
        assert prep_module.classify_depth(item) == "analytical"

    def test_long_thread_with_one_signal(self, prep_module):
        # Signal must be within first 3000 chars of thread (or in subject)
        item = {
            "classification": "info_received",
            "subject": "Proposal updates",
            "thread": "x" * 5001,
        }
        assert prep_module.classify_depth(item) == "analytical"

    def test_simple_email_is_quick(self, prep_module):
        item = {
            "classification": "fyi",
            "subject": "Quick update",
            "thread": "Hey, just wanted to let you know I'll be late tomorrow.",
        }
        assert prep_module.classify_depth(item) == "quick"

    def test_no_signals_is_quick(self, prep_module):
        item = {
            "classification": "question",
            "subject": "Can we reschedule?",
            "thread": "Hi, is Thursday at 3pm possible instead?",
        }
        assert prep_module.classify_depth(item) == "quick"

    def test_one_signal_short_thread_is_quick(self, prep_module):
        item = {
            "classification": "info_received",
            "subject": "Invoice attached",
            "thread": "Please find attached.",
        }
        assert prep_module.classify_depth(item) == "quick"

    def test_percentage_counts_as_signal(self, prep_module):
        item = {
            "classification": "info_received",
            "subject": "Quarterly forecast update",
            "thread": "Growth is at 25% with strong forecast numbers.",
        }
        assert prep_module.classify_depth(item) == "analytical"

    def test_missing_fields_default_to_quick(self, prep_module):
        item = {}
        assert prep_module.classify_depth(item) == "quick"


# ---------------------------------------------------------------------------
# fetch_twenty_person returns tuple
# ---------------------------------------------------------------------------


class TestFetchTwentyPerson:
    def test_returns_tuple_for_none(self, prep_module):
        result = prep_module.fetch_twenty_person(None)
        assert isinstance(result, tuple)
        assert len(result) == 2
        data, person_id = result
        assert data == {}
        assert person_id is None

    def test_returns_person_id(self, prep_module, _mock_crm_dal):
        mock_pg = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_pg.connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("uuid-123",)

        _mock_crm_dal.get_person.return_value = {
            "jobTitle": "CEO",
            "city": "NYC",
            "company": {"name": "Acme"},
        }

        with patch.dict(sys.modules, {"psycopg2": mock_pg}):
            data, person_id = prep_module.fetch_twenty_person("alice@example.com")

        assert person_id == "uuid-123"
        assert data["jobTitle"] == "CEO"
        assert data["company"] == "Acme"


# ---------------------------------------------------------------------------
# enrich_item integration
# ---------------------------------------------------------------------------


class TestEnrichItem:
    def test_enriched_item_has_all_fields(self, prep_module):
        with (
            patch.object(prep_module, "fetch_email_thread", return_value="Thread text here"),
            patch.object(prep_module, "fetch_twenty_person", return_value=({}, None)),
            patch.object(prep_module, "fetch_memory_facts", return_value=[]),
            patch.object(prep_module, "enrich_topic_rag", return_value=[]),
            patch.object(prep_module, "enrich_calendar_context", return_value=[]),
            patch.object(prep_module, "enrich_crm_history", return_value=[]),
        ):
            item = {
                "threadId": "thread-1",
                "from": "Alice <alice@example.com>",
                "subject": "Test",
                "classification": "fyi",
                "contact": {"name": "Alice"},
            }
            result = prep_module.enrich_item(item)

        assert "thread" in result
        assert "topicContext" in result
        assert "calendarContext" in result
        assert "crmHistory" in result
        assert "depth" in result
        assert result["repliedAt"] is None
        assert result["depth"] in ("quick", "analytical")

    def test_analytical_item_gets_analytical_depth(self, prep_module):
        with (
            patch.object(
                prep_module, "fetch_email_thread", return_value="Revenue $50K forecast 20%"
            ),
            patch.object(prep_module, "fetch_twenty_person", return_value=({}, "person-1")),
            patch.object(prep_module, "fetch_memory_facts", return_value=[]),
            patch.object(
                prep_module, "enrich_topic_rag", return_value=[{"text": "fact", "score": 0.9}]
            ),
            patch.object(prep_module, "enrich_calendar_context", return_value=[]),
            patch.object(
                prep_module,
                "enrich_crm_history",
                return_value=[{"channel": "Email", "status": "open", "lastActivity": "2026-02-20"}],
            ),
        ):
            item = {
                "threadId": "thread-2",
                "from": "Bob <bob@example.com>",
                "subject": "Q1 Revenue Report",
                "classification": "analytical",
                "contact": {"name": "Bob"},
            }
            result = prep_module.enrich_item(item)

        assert result["depth"] == "analytical"
        assert len(result["topicContext"]) == 1
        assert len(result["crmHistory"]) == 1

    def test_crm_history_called_with_person_id(self, prep_module):
        mock_crm_hist = MagicMock(return_value=[])
        with (
            patch.object(prep_module, "fetch_email_thread", return_value=None),
            patch.object(prep_module, "fetch_twenty_person", return_value=({}, "person-42")),
            patch.object(prep_module, "fetch_memory_facts", return_value=[]),
            patch.object(prep_module, "enrich_topic_rag", return_value=[]),
            patch.object(prep_module, "enrich_calendar_context", return_value=[]),
            patch.object(prep_module, "enrich_crm_history", mock_crm_hist),
        ):
            item = {
                "threadId": "thread-3",
                "from": "Carol <carol@example.com>",
                "subject": "Hello",
                "classification": "fyi",
                "contact": {"name": "Carol"},
            }
            prep_module.enrich_item(item)

        mock_crm_hist.assert_called_once_with("person-42")


# ---------------------------------------------------------------------------
# filter_already_replied
# ---------------------------------------------------------------------------


class TestFilterAlreadyReplied:
    def test_strips_replied_threads(self, prep_module, tmp_path):
        email_log = {
            "entries": {
                "thread-1": {"actionCompletedAt": "2026-02-20T10:00:00Z"},
                "thread-2": {"actionCompletedAt": None},
            }
        }
        log_path = tmp_path / "email-log.json"
        log_path.write_text(json.dumps(email_log))

        items = [
            {"threadId": "thread-1", "subject": "Already replied"},
            {"threadId": "thread-2", "subject": "Not replied yet"},
            {"threadId": "thread-3", "subject": "Unknown thread"},
        ]

        with patch.object(prep_module, "EMAIL_LOG_PATH", str(log_path)):
            result = prep_module.filter_already_replied(items)

        assert len(result) == 2
        assert result[0]["threadId"] == "thread-2"
        assert result[1]["threadId"] == "thread-3"

    def test_keeps_unreplied_threads(self, prep_module, tmp_path):
        email_log = {
            "entries": {
                "thread-1": {"actionCompletedAt": None},
                "thread-2": {},
            }
        }
        log_path = tmp_path / "email-log.json"
        log_path.write_text(json.dumps(email_log))

        items = [
            {"threadId": "thread-1", "subject": "Pending"},
            {"threadId": "thread-2", "subject": "Also pending"},
        ]

        with patch.object(prep_module, "EMAIL_LOG_PATH", str(log_path)):
            result = prep_module.filter_already_replied(items)

        assert len(result) == 2

    def test_handles_missing_log(self, prep_module, tmp_path):
        items = [
            {"threadId": "thread-1", "subject": "Test"},
        ]

        with patch.object(prep_module, "EMAIL_LOG_PATH", str(tmp_path / "nonexistent.json")):
            result = prep_module.filter_already_replied(items)

        assert len(result) == 1
