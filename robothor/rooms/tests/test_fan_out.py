"""``fan_out_edition``: who hears that an edition is out, and what they hear.

Stories: H3 (each member gets a short summary and the workspace link in their
main conversation, never the full content; a muted member and a preview get
nothing), and the notification shape the dashboard reads: type ``info``,
addressed to the user id, ``metadata`` carrying ``room_id`` and
``edition_id`` and never the session key. A failed recipient does not stop
the others, an external channel is used when the member chose one and has it
linked, and one ``room.edition`` event goes out with ids only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.channels.base import SendReceipt
from robothor.engine.channels.webchat import WebchatChannel
from robothor.rooms import dal, definition, editions, fanout
from robothor.rooms.access import Actor
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401

pytestmark = pytest.mark.integration

REAL_SEND = WebchatChannel.send
LONG = "Revenue rose 4% on the day. " * 200


@pytest.fixture
def env(staff_db, monkeypatch):  # noqa: F811
    from robothor.crm import dal as crm_dal
    from robothor.engine import chat_store
    from robothor.engine.channels import identities

    for module in (crm_dal, chat_store, identities, fanout):
        monkeypatch.setattr(module, "get_connection", committed_connection)
    sent: list[dict[str, Any]] = []

    async def fake_send(self: Any, target: str, text: str, **kw: Any) -> SendReceipt:
        sent.append({"target": target, "text": text, **kw})
        if target == getattr(env_ns, "broken", None):
            raise RuntimeError("inbox down")
        return SendReceipt(acknowledged=2, expected=2, target=target)

    monkeypatch.setattr(WebchatChannel, "send", fake_send)
    events: list[tuple[Any, Any]] = []
    monkeypatch.setattr(editions, "emit_edition_event", lambda r, e: events.append((r, e)))
    env_ns = SimpleNamespace(db=staff_db, sent=sent, events=events, broken=None)
    try:
        yield env_ns
    finally:
        staff_db.cur.execute(
            "DELETE FROM crm_agent_notifications WHERE tenant_id = %s", (staff_db.tenant,)
        )
        staff_db.cur.execute(
            "DELETE FROM user_channel_identities WHERE tenant_id = %s", (staff_db.tenant,)
        )


def _room_and_edition(db: Any, *, preview: bool = False) -> tuple[Any, Any]:
    alice = Actor(user_id=db.alice, role="member", tenant_id=db.tenant)
    room = definition.create_room_with_schedule(
        alice,
        kind="report",
        title="Daily sales",
        instructions="Summarise sales.",
        agent_id="main",
        share_with=[db.bob, db.carol, db.vera],
    )
    from datetime import date

    building, _ = dal.create_edition(
        db.tenant,
        room.id,
        grain="day",
        period_start=date(2026, 11, 20),
        period_end=date(2026, 11, 20),
        preview=preview,
        requested_by=db.alice,
    )
    edition = dal.publish_edition(
        db.tenant,
        building.id,
        title="Sales for 20 November",
        summary=LONG,
        blocks=[{"component": "render_markdown", "props": {"content": "SECRET TABLE"}}],
    )
    return room, edition


class TestWhoHears:
    async def test_every_member_with_access_gets_a_note(self, env):
        room, edition = _room_and_edition(env.db)

        receipts = await fanout.fan_out_edition(room, edition)

        targets = {s["target"] for s in env.sent}
        assert targets == {env.db.alice, env.db.bob, env.db.carol, env.db.vera}
        assert len(receipts) == 4

    async def test_a_muted_member_gets_no_note(self, env):
        room, edition = _room_and_edition(env.db)
        dal.set_notify(env.db.tenant, room.id, env.db.carol, {"muted": True})

        await fanout.fan_out_edition(room, edition)

        assert env.db.carol not in {s["target"] for s in env.sent}

    async def test_a_room_that_notifies_nobody_by_default_reaches_only_opted_in(self, env):
        room, edition = _room_and_edition(env.db)
        room = dal.update_definition(
            env.db.tenant, room.id, {"settings": {"notify_default": False}}, env.db.alice, room.rev
        )
        dal.set_notify(env.db.tenant, room.id, env.db.bob, {"muted": False})

        await fanout.fan_out_edition(room, edition)

        assert {s["target"] for s in env.sent} == {env.db.bob}

    async def test_a_deactivated_member_gets_no_note(self, env):
        room, edition = _room_and_edition(env.db)
        env.db.cur.execute(
            "UPDATE user_accounts SET status = 'disabled' WHERE id = %s", (env.db.bob,)
        )

        await fanout.fan_out_edition(room, edition)

        assert env.db.bob not in {s["target"] for s in env.sent}

    async def test_named_recipients_only(self, env):
        room, edition = _room_and_edition(env.db)

        await fanout.fan_out_edition(room, edition, recipients=[env.db.bob])

        assert [s["target"] for s in env.sent] == [env.db.bob]

    async def test_a_named_recipient_without_access_is_skipped(self, env):
        room, edition = _room_and_edition(env.db)

        await fanout.fan_out_edition(room, edition, recipients=[env.db.olga])

        assert env.sent == []

    async def test_a_preview_is_never_fanned_out(self, env):
        room, edition = _room_and_edition(env.db, preview=True)

        assert await fanout.fan_out_edition(room, edition) == []
        assert env.sent == [] and env.events == []

    async def test_a_failed_recipient_does_not_stop_the_others(self, env):
        room, edition = _room_and_edition(env.db)
        env.broken = env.db.bob

        receipts = await fanout.fan_out_edition(room, edition)

        assert len(receipts) == 4
        failed = [r for r in receipts if r.target == env.db.bob]
        assert failed and failed[0].status and failed[0].status.startswith("failed:")
        assert sum(1 for r in receipts if r.status is None) == 3


class TestWhatTheyHear:
    async def test_a_short_summary_and_the_workspace_link(self, env):
        room, edition = _room_and_edition(env.db)

        await fanout.fan_out_edition(room, edition)

        text = env.sent[0]["text"]
        assert f"[{room.title}](/?v=chat&room={room.id})" in text
        assert len(text) <= fanout.MAX_NOTE_CHARS + len(room.title) + 64
        assert "SECRET TABLE" not in text
        assert LONG not in text
        assert room.session_key not in str(env.sent)

    async def test_the_metadata_names_the_room_and_edition_never_the_key(self, env):
        room, edition = _room_and_edition(env.db)

        await fanout.fan_out_edition(room, edition)

        metadata = env.sent[0]["metadata"]
        assert metadata["room_id"] == room.id
        assert metadata["edition_id"] == edition.id
        assert room.session_key not in str(metadata)
        assert env.sent[0]["tenant_id"] == env.db.tenant

    async def test_one_event_with_ids_only(self, env):
        room, edition = _room_and_edition(env.db)

        await fanout.fan_out_edition(room, edition)

        assert len(env.events) == 1
        assert editions.edition_event_payload(*env.events[0]) == {
            "room_id": room.id,
            "tenant_id": env.db.tenant,
            "edition_id": edition.id,
        }

    async def test_no_event_when_the_publish_already_sent_it(self, env):
        room, edition = _room_and_edition(env.db)
        await fanout.fan_out_edition(room, edition, emit_event=False)
        assert env.events == []


class TestExternalChannel:
    async def test_a_linked_channel_the_member_chose_is_used(self, env, monkeypatch):
        room, edition = _room_and_edition(env.db)
        dal.set_notify(env.db.tenant, room.id, env.db.bob, {"channel": "telegram"})
        env.db.cur.execute(
            "INSERT INTO user_channel_identities (tenant_id, channel, user_id, native_id, "
            "display_name, role, paired_by) VALUES (%s, 'telegram', %s, '424242', 'Bob', "
            "'member', 'operator:test')",
            (env.db.tenant, env.db.bob),
        )
        external: list[tuple[str, str]] = []

        class Telegram:
            async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
                external.append((target, text))
                return SendReceipt(acknowledged=1, expected=1, target=target)

        monkeypatch.setattr(
            "robothor.engine.channels.registry.get_channel",
            lambda name: Telegram() if name == "telegram" else None,
        )

        receipts = await fanout.fan_out_edition(room, edition)

        assert [t for t, _ in external] == ["424242"]
        assert "SECRET TABLE" not in external[0][1]
        assert len(receipts) == 5

    async def test_an_unlinked_channel_is_a_failed_receipt_not_a_crash(self, env, monkeypatch):
        room, edition = _room_and_edition(env.db)
        dal.set_notify(env.db.tenant, room.id, env.db.bob, {"channel": "telegram"})

        receipts = await fanout.fan_out_edition(room, edition)

        unlinked = [r for r in receipts if r.status == "failed:room_channel_not_linked"]
        assert len(unlinked) == 1


class TestTheRealInboxRow:
    async def test_the_row_the_dashboard_reads(self, env, monkeypatch):
        """Through the real webchat channel: type info, to the user id, ids in metadata."""
        from robothor.engine.channels import webchat

        monkeypatch.setattr(WebchatChannel, "send", REAL_SEND)
        monkeypatch.setattr(
            webchat,
            "_resolve_identity",
            lambda channel, user, tenant: SimpleNamespace(role="member", display_name=""),
        )
        room, edition = _room_and_edition(env.db)

        await fanout.fan_out_edition(room, edition, recipients=[env.db.bob])

        env.db.cur.execute(
            "SELECT to_agent, notification_type, metadata, body FROM crm_agent_notifications "
            "WHERE tenant_id = %s",
            (env.db.tenant,),
        )
        ((to, kind, metadata, body),) = env.db.cur.fetchall()
        assert (to, kind) == (env.db.bob, "info")
        assert metadata["room_id"] == room.id and metadata["edition_id"] == edition.id
        assert room.session_key not in str(metadata)
        assert f"(/?v=chat&room={room.id})" in body
        env.db.cur.execute(
            "SELECT session_key FROM chat_sessions WHERE tenant_id = %s", (env.db.tenant,)
        )
        keys = {r[0] for r in env.db.cur.fetchall()}
        assert f"agent:main:user:{env.db.bob}" in keys  # the main conversation
        assert room.session_key not in keys
