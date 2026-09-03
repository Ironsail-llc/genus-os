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

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


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


# ── The SQL itself ───────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(db_dsn):
    """Override the shared fixture so an ABSENT database skips this module.

    ``tests/conftest_integration.py``'s version connects unconditionally, which
    turns "no integration database on this machine" into a red build rather
    than a skip. The distinction matters: a missing test database is not a
    defect in the code under test, and a suite that goes red for it teaches
    people to stop reading red.
    """
    import psycopg2

    try:
        conn = psycopg2.connect(db_dsn)
    except psycopg2.Error as exc:
        pytest.skip(
            f"integration database unreachable ({type(exc).__name__}) — set ROBOTHOR_TEST_DB_DSN"
        )
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.integration
def test_all_three_arms_of_the_lookup_match_against_a_real_database(
    db_cursor, db_conn, mock_get_connection
):
    """The mocked tests above assert the SQL is ISSUED. This asserts it WORKS.

    ``do_not_contact_emails`` is a three-arm UNION — primary address, the
    ``additional_emails`` JSONB list, and ``contact_identifiers`` on the email
    channel — and every arm of it is the difference between an opt-out that
    holds and one that is sidestepped by replying to the address the person
    actually writes from. Not one of those arms had ever been executed: a
    ``jsonb_array_elements`` LATERAL that never runs is a plausible-looking
    string, and a guard built on a query nobody has run is the inert-control
    shape this repo keeps shipping.

    Skips rather than fails when the integration database is absent or has not
    had migration 113 applied — a missing test database is not a defect in
    this code, and a test that turns one into a red build teaches people to
    ignore red builds.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.crm.dal import do_not_contact_emails

    db_cursor.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'crm_people' AND column_name = 'do_not_contact'"
    )
    if db_cursor.fetchone() is None:
        pytest.skip(
            "crm_people.do_not_contact is absent — apply migration 113 to the "
            "integration database (`robothor migrate`) to run this test"
        )

    flagged = str(uuid.uuid4())
    willing = str(uuid.uuid4())
    db_cursor.execute(
        """
        INSERT INTO crm_people
            (id, tenant_id, first_name, last_name, email, additional_emails,
             do_not_contact)
        VALUES (%s, %s, 'Dana', 'Optout', 'dana@example.com',
                '["dana.optout@example.net"]'::jsonb, TRUE)
        """,
        (flagged, DEFAULT_TENANT),
    )
    db_cursor.execute(
        """
        INSERT INTO contact_identifiers (tenant_id, channel, identifier, person_id)
        VALUES (%s, 'email', 'Dana.Work@Example.com', %s)
        """,
        (DEFAULT_TENANT, flagged),
    )
    # A second, unflagged person proves the filter is the flag and not the
    # query merely returning everything it was handed.
    db_cursor.execute(
        """
        INSERT INTO crm_people (id, tenant_id, first_name, last_name, email)
        VALUES (%s, %s, 'Eli', 'Willing', 'eli@example.com')
        """,
        (willing, DEFAULT_TENANT),
    )
    # No commit. The DAL reads through `mock_get_connection`, which hands it
    # THIS connection, so the uncommitted INSERTs above are visible to it —
    # and the fixture's teardown rollback is then able to undo them. A
    # `db_conn.commit()` here goes to the raw connection, not the no-commit
    # wrapper, so it lands for real and leaves these rows in the test database
    # after every run.
    blocked = do_not_contact_emails(
        [
            "DANA@example.com",  # primary, mixed case
            "dana.optout@example.net",  # additional_emails
            "dana.work@example.com",  # contact_identifiers
            "eli@example.com",  # a person who did not opt out
            "stranger@example.org",  # nobody at all
        ],
        tenant_id=DEFAULT_TENANT,
    )

    assert blocked == {
        "dana@example.com",
        "dana.optout@example.net",
        "dana.work@example.com",
    }
