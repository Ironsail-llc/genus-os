"""``crm_people.do_not_contact`` — the DAL half of the outreach opt-out.

Migration 113 adds the column; these tests pin that something actually reads
and writes it. The mocked-connection style matches the rest of the DAL suite
(``test_dal_tasks.py``, ``test_dal_data_scoping.py``): the assertions are on
the SQL and parameters the DAL issues, so no database is required.

The one behaviour worth calling out is ``update_person(do_not_contact=False)``.
``update_person`` skips ``None`` fields, and a boolean flag is exactly the
field where "skip falsy" instead of "skip None" would make the un-setting path
silently unavailable — you could mark someone do-not-contact and never take it
back. The False case is tested for that reason.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _mock_conn(fetchall_return: list[Any] | None = None, rowcount: int = 1):
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = fetchall_return if fetchall_return is not None else []
    mock_cur.fetchone.return_value = None
    mock_cur.rowcount = rowcount

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestPersonShape:
    def test_flag_is_exposed_on_the_read_model(self):
        from robothor.crm.models import person_to_dict

        row = {"id": "p1", "first_name": "Alice", "do_not_contact": True}
        assert person_to_dict(row)["doNotContact"] is True

    def test_defaults_false_when_the_column_is_absent(self):
        """A row read before the migration applied must not read as opted out."""
        from robothor.crm.models import person_to_dict

        assert person_to_dict({"id": "p1", "first_name": "Alice"})["doNotContact"] is False


class TestUpdatePerson:
    @patch("robothor.crm.dal.get_connection")
    def test_sets_the_flag(self, mock_get_conn):
        from robothor.crm.dal import update_person

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        with patch("robothor.crm.dal._safe_audit") as audit:
            assert update_person("p1", tenant_id="t1", do_not_contact=True) is True

        sql, params = mock_cur.execute.call_args[0]
        assert "do_not_contact = %s" in sql
        assert True in params
        # The change is on the audit record like every other person edit.
        assert "do_not_contact" in audit.call_args.kwargs["details"]["fields"]

    @patch("robothor.crm.dal.get_connection")
    def test_clears_the_flag(self, mock_get_conn):
        """False is a value, not an omission — the opt-out must be reversible."""
        from robothor.crm.dal import update_person

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        with patch("robothor.crm.dal._safe_audit"):
            assert update_person("p1", tenant_id="t1", do_not_contact=False) is True

        sql, params = mock_cur.execute.call_args[0]
        assert "do_not_contact = %s" in sql
        assert False in params

    @patch("robothor.crm.dal.get_connection")
    def test_round_trip_through_the_read_model(self, mock_get_conn):
        """set -> read: what update writes is what get_person hands back."""
        from robothor.crm.dal import get_person, update_person

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        with patch("robothor.crm.dal._safe_audit"):
            update_person("p1", tenant_id="t1", do_not_contact=True)
        written = dict(zip(["do_not_contact"], mock_cur.execute.call_args[0][1], strict=False))

        mock_cur.fetchone.return_value = {
            "id": "p1",
            "first_name": "Alice",
            "do_not_contact": written["do_not_contact"],
        }
        person = get_person("p1", tenant_id="t1")
        assert person is not None
        assert person["doNotContact"] is True


class TestDoNotContactEmails:
    @patch("robothor.crm.dal.get_connection")
    def test_returns_only_the_flagged_addresses(self, mock_get_conn):
        from robothor.crm.dal import do_not_contact_emails

        mock_conn, mock_cur = _mock_conn(fetchall_return=[{"addr": "bob@example.com"}])
        mock_get_conn.return_value = mock_conn

        blocked = do_not_contact_emails(["Alice@example.com", "bob@example.com"], tenant_id="t1")
        assert blocked == {"bob@example.com"}

        sql, params = mock_cur.execute.call_args[0]
        assert "do_not_contact" in sql
        assert "deleted_at IS NULL" in sql
        assert "contact_identifiers" in sql
        assert "additional_emails" in sql
        # Addresses are compared case-insensitively — mail is not case sensitive
        # in the local part in practice, and the CRM stores lowercased email.
        assert ["alice@example.com", "bob@example.com"] in params
        assert "t1" in params

    @patch("robothor.crm.dal.get_connection")
    def test_no_recipients_issues_no_query(self, mock_get_conn):
        from robothor.crm.dal import do_not_contact_emails

        assert do_not_contact_emails([], tenant_id="t1") == set()
        mock_get_conn.assert_not_called()

    @patch("robothor.crm.dal.get_connection")
    def test_blank_and_duplicate_addresses_are_normalised_away(self, mock_get_conn):
        from robothor.crm.dal import do_not_contact_emails

        mock_conn, mock_cur = _mock_conn()
        mock_get_conn.return_value = mock_conn

        do_not_contact_emails(["  Bob@example.com ", "bob@example.com", "", None], tenant_id="t1")

        params = mock_cur.execute.call_args[0][1]
        assert ["bob@example.com"] in params
