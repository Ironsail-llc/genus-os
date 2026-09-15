"""Where a channel remembers how to reach back — and why it is not the identity table.

A proactive message on Bot-Framework-shaped surfaces (Teams is the first) needs
a *conversation reference*: the tenant-specific ``serviceUrl`` and the
conversation id the platform minted when somebody first spoke. Neither is
derivable from the person's id, and both arrive only on an inbound activity.

``user_channel_identities`` is the wrong home for it and the rest of this file
exists to keep it that way:

* a reference has to be recorded for a sender who is **not paired** — the
  pairing code itself is a reply, and there is nowhere to send it otherwise —
  while an identity row is a *grant* that only an operator may create;
* a reference is routing, not authorization. Storing it beside ``role`` and
  ``paired_by`` would put a value the sender's own platform supplies one column
  away from the column that decides what they may do.

So: a separate table, and a store that will not write a ``serviceUrl`` that is
not HTTPS — the one field on the row that an attacker would most like to choose.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from robothor.engine.channels import conversations

TENANT = "00000000-0000-0000-0000-000000000000"
CHANNEL = "teams"
ALICE = "29:alice-object-id"


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        rows, self.rows = self.rows, []
        return rows


class _Connection:
    def __init__(self, rows: list[dict[str, Any] | None] | None = None) -> None:
        self.cursor_obj = _Cursor(list(rows or []))

    def cursor(self, **_kw: Any) -> _Cursor:
        return self.cursor_obj


@pytest.fixture
def db(monkeypatch):
    """A recording stand-in for the one connection this module opens."""
    holder: dict[str, _Connection] = {"conn": _Connection()}

    def _install(rows: list[dict[str, Any] | None] | None = None) -> _Connection:
        holder["conn"] = _Connection(rows)
        return holder["conn"]

    @contextlib.contextmanager
    def _get_connection():
        yield holder["conn"]

    monkeypatch.setattr(conversations, "get_connection", _get_connection)
    return _install


def _sql(conn: _Connection) -> str:
    return " ".join(sql for sql, _params in conn.cursor_obj.calls)


class TestRecording:
    def test_a_reference_is_upserted_for_the_tenant_channel_and_sender(self, db):
        conn = db()
        conversations.record(
            CHANNEL,
            ALICE,
            tenant_id=TENANT,
            conversation_id="19:meeting@thread.v2",
            service_url="https://smba.trafficmanager.net/emea/",
        )
        sql = _sql(conn)
        assert "INSERT INTO channel_conversation_refs" in sql
        assert "ON CONFLICT" in sql, "a second message from the same sender would duplicate the row"
        params = conn.cursor_obj.calls[0][1]
        assert params[0] == TENANT
        assert CHANNEL in params and ALICE in params

    def test_a_service_url_that_is_not_https_is_refused_before_any_sql(self, db):
        """The one field on this row the sender's platform supplies. A stored
        ``http://`` reference would send the next proactive message — which
        carries a bearer token — over the wire in the clear."""
        conn = db()
        with pytest.raises(ValueError):
            conversations.record(
                CHANNEL,
                ALICE,
                tenant_id=TENANT,
                conversation_id="19:x",
                service_url="http://attacker.example.com/",
            )
        assert conn.cursor_obj.calls == [], "the row was written anyway"

    def test_an_empty_conversation_id_is_refused(self, db):
        conn = db()
        with pytest.raises(ValueError):
            conversations.record(
                CHANNEL, ALICE, tenant_id=TENANT, conversation_id="", service_url="https://a.test/"
            )
        assert conn.cursor_obj.calls == []


class TestReading:
    def test_a_known_sender_resolves_to_its_reference(self, db):
        db(
            [
                {
                    "channel": CHANNEL,
                    "native_id": ALICE,
                    "conversation_id": "19:meeting@thread.v2",
                    "service_url": "https://smba.trafficmanager.net/emea/",
                    "display_name": "Alice",
                    "tenant_id": TENANT,
                }
            ]
        )
        ref = conversations.reference(CHANNEL, ALICE, tenant_id=TENANT)
        assert ref is not None
        assert ref.conversation_id == "19:meeting@thread.v2"
        assert ref.service_url == "https://smba.trafficmanager.net/emea/"

    def test_an_unknown_sender_is_none_and_not_an_exception(self, db):
        """``None`` is the answer the channel turns into
        ``failed:teams_no_conversation_reference`` — loud, and never a guess at
        somebody else's conversation."""
        db([])
        assert conversations.reference(CHANNEL, "29:nobody", tenant_id=TENANT) is None

    def test_the_lookup_is_scoped_to_tenant_and_channel(self, db):
        conn = db([])
        conversations.reference(CHANNEL, ALICE, tenant_id=TENANT)
        sql, params = conn.cursor_obj.calls[0]
        assert "tenant_id = %s" in sql and "channel = %s" in sql and "native_id = %s" in sql
        assert params == (TENANT, CHANNEL, ALICE)

    def test_a_conversation_id_also_resolves_a_reference(self, db):
        """A target named as a conversation id — how an operator writes
        ``delivery.to`` for a channel-wide post — must resolve too, or a
        proactive send to a room is impossible."""
        conn = db(
            [
                # The sender-id lookup misses: an operator named the room, not
                # the person. The conversation-id lookup is what answers.
                None,
                {
                    "channel": CHANNEL,
                    "native_id": ALICE,
                    "conversation_id": "19:room@thread.v2",
                    "service_url": "https://smba.trafficmanager.net/emea/",
                    "display_name": "",
                    "tenant_id": TENANT,
                },
            ]
        )
        ref = conversations.reference_for_target(CHANNEL, "19:room@thread.v2", tenant_id=TENANT)
        assert ref is not None and ref.conversation_id == "19:room@thread.v2"
        assert "conversation_id = %s" in _sql(conn)


class TestItIsNotAnAuthorization:
    def test_the_row_carries_no_grant_column(self):
        """A reference says *where*, never *who may*. The moment this row grows
        a ``role`` or a ``paired_by``, something can be granted access by
        sending a message — which is the whole of what pairing prevents."""
        assert set(conversations.COLUMNS).isdisjoint({"role", "paired_by", "user_id"})

    def test_every_statement_names_this_table_and_no_other(self, db):
        """Nothing here may reach into the grant tables — not to enrich a
        lookup, not to 'helpfully' create a binding."""
        conn = db([])
        conversations.reference(CHANNEL, ALICE, tenant_id=TENANT)
        conversations.record(
            CHANNEL,
            ALICE,
            tenant_id=TENANT,
            conversation_id="19:x",
            service_url="https://a.test/",
        )
        for sql, _params in conn.cursor_obj.calls:
            assert conversations.TABLE in sql
            assert "user_channel_identities" not in sql
            assert "channel_pairing_codes" not in sql
