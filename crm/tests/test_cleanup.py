"""
Tests for the one-time CRM cleanup script.

All database operations are mocked — no real DB connections.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add cleanup script's parent to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import cleanup_crm


def _mock_person_row(pid, first_name, last_name, **extras):
    """Create a mock crm_people DB row."""
    base = {
        "id": pid,
        "first_name": first_name,
        "last_name": last_name,
        "email": extras.get("email"),
        "phone": extras.get("phone"),
        "additional_emails": extras.get("additional_emails"),
        "additional_phones": extras.get("additional_phones"),
        "job_title": extras.get("job_title", ""),
        "city": extras.get("city", ""),
        "avatar_url": "",
        "linkedin_url": "",
        "company_id": extras.get("company_id"),
        "company_name": extras.get("company_name"),
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "deleted_at": None,
    }
    return base


def _mock_company_row(cid, name, **extras):
    """Create a mock crm_companies DB row."""
    return {
        "id": cid,
        "name": name,
        "domain_name": extras.get("domain_name", ""),
        "employees": extras.get("employees"),
        "address_street1": "",
        "address_city": "",
        "address_state": "",
        "linkedin_url": "",
        "ideal_customer_profile": False,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "deleted_at": None,
    }


# ─── Junk Detection ──────────────────────────────────────────────────


class TestFindJunkPeople:
    def test_finds_furniture_names(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            {"id": "id-1", "first_name": "couch", "last_name": ""},
            {"id": "id-2", "first_name": "Philip", "last_name": "D'Agostino"},
            {"id": "id-3", "first_name": "chair", "last_name": ""},
        ]

        result = cleanup_crm._find_junk_people(mock_conn)
        assert "id-1" in result
        assert "id-3" in result
        assert "id-2" not in result

    def test_finds_system_accounts(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            {"id": "id-1", "first_name": "Vision Monitor", "last_name": "System"},
            {"id": "id-2", "first_name": "Email", "last_name": "Responder"},
        ]

        result = cleanup_crm._find_junk_people(mock_conn)
        assert "id-1" in result
        assert "id-2" in result

    def test_finds_via_google_docs(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            {"id": "id-1", "first_name": "Philip D'Agostino",
             "last_name": "(via Google Docs)"},
        ]

        result = cleanup_crm._find_junk_people(mock_conn)
        assert "id-1" in result


class TestFindJunkCompanies:
    def test_finds_null_company(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            {"id": "co-1", "name": "null"},
            {"id": "co-2", "name": "Ironsail"},
        ]

        result = cleanup_crm._find_junk_companies(mock_conn)
        assert "co-1" in result
        assert "co-2" not in result

    def test_finds_system_companies(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            {"id": "co-1", "name": "Robothor System"},
            {"id": "co-2", "name": "OpenRouter"},
        ]

        result = cleanup_crm._find_junk_companies(mock_conn)
        assert "co-1" in result
        assert "co-2" in result


# ─── Data Quality ─────────────────────────────────────────────────────


class TestDataQuality:
    def test_fixes_null_strings(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        # First call: city nulls, second: job_title nulls, third: email-as-name
        mock_cur.fetchall.side_effect = [
            [{"id": "id-1", "city": "null"}],     # city
            [],                                     # job_title
            [{"id": "id-2", "email": "John Doe"}], # email-as-name
        ]

        fixes = cleanup_crm._fix_data_quality(mock_conn, dry_run=False)
        assert fixes == 2

    def test_dry_run_no_writes(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        mock_cur.fetchall.side_effect = [
            [{"id": "id-1", "city": "null"}],
            [],
            [],
        ]

        fixes = cleanup_crm._fix_data_quality(mock_conn, dry_run=True)
        assert fixes == 1
        mock_conn.commit.assert_not_called()


# ─── Full Cleanup (integration-style with mocks) ─────────────────────


class TestRunCleanup:
    @patch("cleanup_crm._fix_data_quality", return_value=3)
    @patch("cleanup_crm.crm_dal")
    @patch("cleanup_crm._find_junk_companies", return_value=["co-1", "co-2"])
    @patch("cleanup_crm._find_junk_people", return_value=["p-1", "p-2", "p-3"])
    @patch("cleanup_crm._backup_data", return_value={"crm_people": []})
    @patch("psycopg2.connect")
    @patch("builtins.open", create=True)
    @patch("cleanup_crm.Path")
    def test_dry_run_returns_counts(self, mock_path, mock_open, mock_pg,
                                     mock_backup, mock_junk_p, mock_junk_c,
                                     mock_dal, mock_quality):
        # Setup path mocking
        mock_backup_path = MagicMock()
        mock_backup_path.exists.return_value = True  # Skip backup
        mock_path.return_value.__truediv__ = MagicMock(return_value=mock_backup_path)

        # Mock psycopg2 cursor for pre/post counts
        mock_cur = MagicMock()
        mock_pg.return_value.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = {"c": 93}

        # Mock crm_dal functions
        mock_dal.get_person.return_value = None  # Skip merges (keepers not found)
        mock_dal.get_company.return_value = None
        mock_dal.config.PG_DSN = "test_dsn"

        result = cleanup_crm.run_cleanup(dry_run=True)

        assert result["deleted_people"] == 3
        assert result["deleted_companies"] == 2
        assert result["quality_fixes"] == 3
