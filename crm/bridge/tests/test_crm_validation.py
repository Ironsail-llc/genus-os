"""
Tests for CRM DAL validation, blocklist, and null-scrubbing.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crm_dal


# ─── validate_person_input ─────────────────────────────────────────────


class TestValidatePersonInput:
    def test_rejects_blocklisted_full_name(self):
        ok, reason = crm_dal.validate_person_input("Vision Monitor", "System")
        assert not ok
        assert "blocklist" in reason

    def test_rejects_blocklisted_first_name_only(self):
        ok, reason = crm_dal.validate_person_input("couch", "")
        assert not ok
        assert "blocklist" in reason

    def test_rejects_case_insensitive(self):
        ok, reason = crm_dal.validate_person_input("COUCH", "")
        assert not ok

    def test_rejects_null_string_first_name(self):
        ok, reason = crm_dal.validate_person_input("null", "")
        assert not ok
        assert "null-like" in reason

    def test_rejects_none_string(self):
        ok, reason = crm_dal.validate_person_input("None", "")
        assert not ok

    def test_rejects_short_first_name(self):
        ok, reason = crm_dal.validate_person_input("A", "Smith")
        assert not ok
        assert "2 characters" in reason

    def test_rejects_email_without_at(self):
        ok, reason = crm_dal.validate_person_input("John", "Doe", email="notanemail")
        assert not ok
        assert "@" in reason

    def test_accepts_normal_input(self):
        ok, reason = crm_dal.validate_person_input("Philip", "D'Agostino", email="philip@ironsail.ai")
        assert ok
        assert reason == "ok"

    def test_accepts_no_email(self):
        ok, reason = crm_dal.validate_person_input("John", "Doe")
        assert ok

    def test_accepts_two_char_name(self):
        ok, reason = crm_dal.validate_person_input("Al", "Smith")
        assert ok


# ─── _scrub_null_string ────────────────────────────────────────────────


class TestScrubNullString:
    def test_scrubs_null(self):
        assert crm_dal._scrub_null_string("null") == ""

    def test_scrubs_none_string(self):
        assert crm_dal._scrub_null_string("None") == ""

    def test_scrubs_na(self):
        assert crm_dal._scrub_null_string("N/A") == ""

    def test_scrubs_with_whitespace(self):
        assert crm_dal._scrub_null_string("  null  ") == ""

    def test_passes_normal_string(self):
        assert crm_dal._scrub_null_string("New York") == "New York"

    def test_passes_none(self):
        assert crm_dal._scrub_null_string(None) is None


# ─── create_person with validation ────────────────────────────────────


class TestCreatePersonValidation:
    @patch("crm_dal._conn")
    def test_blocked_name_returns_none(self, mock_conn):
        result = crm_dal.create_person("couch", "")
        assert result is None
        mock_conn.assert_not_called()

    @patch("crm_dal._conn")
    def test_short_name_returns_none(self, mock_conn):
        result = crm_dal.create_person("A", "")
        assert result is None
        mock_conn.assert_not_called()

    @patch("crm_dal._conn")
    def test_bad_email_returns_none(self, mock_conn):
        result = crm_dal.create_person("John", "Doe", email="notanemail")
        assert result is None
        mock_conn.assert_not_called()

    @patch("crm_dal._conn")
    def test_normalizes_email_lowercase(self, mock_conn):
        mock_cur = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cur

        crm_dal.create_person("John", "Doe", email="John@EXAMPLE.COM")

        call_args = mock_cur.execute.call_args[0]
        # The email parameter should be lowercased
        assert call_args[1][3] == "john@example.com"


# ─── find_or_create_company with blocklist ────────────────────────────


class TestFindOrCreateCompanyBlocklist:
    @patch("crm_dal._conn")
    def test_blocked_name_returns_none(self, mock_conn):
        result = crm_dal.find_or_create_company("null")
        assert result is None
        mock_conn.assert_not_called()

    @patch("crm_dal._conn")
    def test_blocked_test_returns_none(self, mock_conn):
        result = crm_dal.find_or_create_company("Test")
        assert result is None
        mock_conn.assert_not_called()


# ─── update_person null scrubbing ─────────────────────────────────────


class TestUpdatePersonScrubbing:
    @patch("crm_dal._conn")
    def test_scrubs_null_city(self, mock_conn):
        mock_cur = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cur
        mock_cur.rowcount = 1

        crm_dal.update_person("test-id", city="null")

        call_args = mock_cur.execute.call_args[0]
        # City should be scrubbed to empty string
        assert "" in call_args[1]

    @patch("crm_dal._conn")
    def test_scrubs_null_job_title(self, mock_conn):
        mock_cur = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cur
        mock_cur.rowcount = 1

        crm_dal.update_person("test-id", job_title="None")

        call_args = mock_cur.execute.call_args[0]
        assert "" in call_args[1]


# ─── update_person additional_emails JSONB ────────────────────────────


class TestUpdatePersonAdditionalEmails:
    @patch("crm_dal._conn")
    def test_sets_additional_emails_jsonb(self, mock_conn):
        mock_cur = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cur
        mock_cur.rowcount = 1

        emails = ["alt1@example.com", "alt2@example.com"]
        result = crm_dal.update_person("test-id", additional_emails=emails)

        assert result is True
        call_sql = mock_cur.execute.call_args[0][0]
        assert "additional_emails" in call_sql
        assert "jsonb" in call_sql

    @patch("crm_dal._conn")
    def test_sets_additional_phones_jsonb(self, mock_conn):
        mock_cur = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cur
        mock_cur.rowcount = 1

        phones = ["+1234567890"]
        result = crm_dal.update_person("test-id", additional_phones=phones)

        assert result is True
        call_sql = mock_cur.execute.call_args[0][0]
        assert "additional_phones" in call_sql
