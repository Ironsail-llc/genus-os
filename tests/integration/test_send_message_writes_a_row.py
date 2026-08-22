"""`send_message` must actually write a row.

``robothor/crm/dal.py`` inserted ``str(uuid.uuid4())`` into ``crm_messages.id``,
which is an INTEGER with a sequence default. Every call raised on the INSERT,
the exception was swallowed, and the caller returned HTTP 200. Measured
2026-08-22 on production:

    SELECT count(*), max(created_at) FROM crm_messages;
     22516 | 2026-04-08 16:08:25

Four and a half months of conversation messages silently discarded, while every
caller was told it worked.

The correct form already existed three files away: the bridge's own DAL
(``crm/bridge/crm_dal.py``) omits ``id`` and lets the sequence assign it.

This is an INTEGRATION test on purpose. A mocked cursor accepts a uuid for an
integer column without complaint — the defect lives in the gap between the code
and the schema, and mocking closes exactly that gap. It is also why the unit
suite never noticed.
"""

from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from robothor.crm import dal  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def conversation():
    """A committed conversation, removed afterwards.

    ``send_message`` opens its OWN connection, so a row sitting uncommitted in
    another transaction is invisible to it and the insert would fail on the
    foreign key for the wrong reason.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crm_conversations (status, inbox_name, tenant_id)
            VALUES ('open', 'send_message contract test', %s)
            RETURNING id
            """,
            (dal.DEFAULT_TENANT,),
        )
        conversation_id = cur.fetchone()[0]
        conn.commit()

    yield conversation_id

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM crm_messages WHERE conversation_id = %s", (conversation_id,))
        cur.execute("DELETE FROM crm_conversations WHERE id = %s", (conversation_id,))
        conn.commit()


def test_send_message_returns_the_created_row(conversation):
    """The defect: this returned None for four and a half months."""
    result = dal.send_message(conversation_id=conversation, content="hello")
    assert result is not None, (
        "send_message returned None — the INSERT raised and was swallowed, "
        "which is how crm_messages stopped receiving rows on 2026-04-08"
    )
    assert result.get("content") == "hello"


def test_the_row_is_actually_in_the_table(conversation):
    """Returning a dict is not the same as persisting one."""
    from robothor.db.connection import get_connection

    dal.send_message(conversation_id=conversation, content="persisted?")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM crm_messages WHERE conversation_id = %s",
            (conversation,),
        )
        rows = [r[0] for r in cur.fetchall()]
    assert "persisted?" in rows


def test_the_id_is_an_integer_from_the_sequence(conversation):
    """A uuid in an integer PK is the whole defect — pin the type."""
    result = dal.send_message(conversation_id=conversation, content="typed")
    assert result is not None
    assert isinstance(result["id"], int), (
        f"crm_messages.id came back as {type(result['id']).__name__}; the column "
        "is INTEGER with a sequence default and the DAL must not supply it"
    )


def test_two_messages_get_distinct_ids(conversation):
    """The sequence, not the caller, owns identity."""
    first = dal.send_message(conversation_id=conversation, content="one")
    second = dal.send_message(conversation_id=conversation, content="two")
    assert first is not None and second is not None
    assert first["id"] != second["id"]
