"""``do_not_contact_phones`` — the SMS half of the outreach opt-out.

``do_not_contact_emails`` guards the email channel; an SMS sender needs the
same guard keyed on phone numbers. Numbers are written every way a human
writes them (``+1 555 010 0100``, ``(555) 010-0100``, ``5550100100``), so the
comparison is on the last ten digits, digits only, on both sides. What comes
back is the caller's own strings — the ones it must not text.

The unit half asserts on the parameters the DAL sends (no database, the DAL
suite's mocked-connection style); the integration half proves the SQL's own
normalisation of stored numbers on PostgreSQL.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mock_conn(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


FORMATS = ["+15550100100", "(555) 010-0100", "5550100100", "+1 555.010.0100"]


@pytest.mark.parametrize("phone", FORMATS)
@patch("robothor.crm.dal.get_connection")
def test_every_format_is_matched_on_its_last_ten_digits(mock_get_conn, phone):
    from robothor.crm.dal import do_not_contact_phones

    conn, cur = _mock_conn([{"digits": "5550100100"}])
    mock_get_conn.return_value = conn

    assert do_not_contact_phones([phone], tenant_id="test-tenant") == {phone}

    sql, params = cur.execute.call_args[0]
    assert "do_not_contact" in sql
    assert "test-tenant" in params
    assert ["5550100100"] in params


@patch("robothor.crm.dal.get_connection")
def test_returns_only_the_opted_out_inputs_as_given(mock_get_conn):
    from robothor.crm.dal import do_not_contact_phones

    conn, _cur = _mock_conn([{"digits": "5550100100"}])
    mock_get_conn.return_value = conn

    got = do_not_contact_phones(["(555) 010-0100", "+15550100100", "555-010-0199"])
    assert got == {"(555) 010-0100", "+15550100100"}


@pytest.mark.parametrize("phones", [[], [""], ["   "], [None], ["12345"]])
@patch("robothor.crm.dal.get_connection")
def test_nothing_to_check_queries_nothing(mock_get_conn, phones):
    from robothor.crm.dal import do_not_contact_phones

    assert do_not_contact_phones(phones) == set()
    mock_get_conn.assert_not_called()


def test_tenant_defaults_to_the_platform_default():
    import inspect

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm.dal import do_not_contact_phones

    default = inspect.signature(do_not_contact_phones).parameters["tenant_id"].default
    assert default == DEFAULT_TENANT


@patch("robothor.crm.dal.get_connection")
def test_a_database_failure_is_not_swallowed(mock_get_conn):
    """A failed lookup must reach the caller: "could not check" is not "nobody opted out"."""
    from robothor.crm.dal import do_not_contact_phones

    mock_get_conn.side_effect = ConnectionError("pool exhausted")
    with pytest.raises(ConnectionError):
        do_not_contact_phones(["5550100100"])


@pytest.mark.integration
def test_stored_numbers_are_normalised_by_the_query(scratch_db):
    import psycopg2

    from robothor.crm.dal import do_not_contact_phones

    db, dsn = scratch_db(through="154_plugin_state")
    with db.cursor() as cur:
        for tenant in ("test-tenant", "tenant-b"):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (tenant, tenant),
            )

    def add(phone, *, opted_out, tenant="test-tenant", additional=None, deleted=False):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_people (id, tenant_id, first_name, last_name, phone, "
                "additional_phones, do_not_contact, deleted_at) "
                "VALUES (%s, %s, 'Alice', 'Example', %s, %s::jsonb, %s, "
                "CASE WHEN %s THEN now() END)",
                (
                    str(uuid.uuid4()),
                    tenant,
                    phone,
                    json.dumps(additional or []),
                    opted_out,
                    deleted,
                ),
            )

    add("+1 (555) 010-0100", opted_out=True)
    add("555.010.0101", opted_out=False)
    add(None, opted_out=True, additional=["(555) 010-0102"])
    add("5550100103", opted_out=True, tenant="tenant-b")
    add("5550100104", opted_out=True, deleted=True)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    with patch("robothor.crm.dal.get_connection", connect):
        got = do_not_contact_phones(
            ["5550100100", "+15550100101", "555-010-0102", "5550100103", "5550100104"],
            tenant_id="test-tenant",
        )
    assert got == {"5550100100", "555-010-0102"}
