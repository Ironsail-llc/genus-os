"""``robothor.crm.interactions.log_interaction``: the core behind the bridge's /log-interaction.

The DAL is mocked: these tests pin the sequence (resolve the contact by its
channel identifier, reuse its newest conversation or open one, append the
message) and the result shape, not SQL.
"""

from __future__ import annotations

from unittest.mock import patch

from robothor.crm import interactions


def _run(**overrides):
    kwargs = {
        "contact_name": "Alice Example",
        "channel": "email",
        "direction": "incoming",
        "content_summary": "Alice Example <alice@example.com>: 'Lunch?'",
        "channel_identifier": "alice@example.com",
        "tenant_id": "tenant-a",
    }
    kwargs.update(overrides)
    return interactions.log_interaction(**kwargs)


def test_resolves_by_identifier_and_appends_to_the_newest_conversation() -> None:
    with (
        patch("robothor.crm.dal.resolve_contact", return_value={"person_id": "p-1"}) as resolve,
        patch(
            "robothor.crm.dal.get_conversations_for_contact", return_value=[{"id": 7}, {"id": 3}]
        ),
        patch("robothor.crm.dal.create_conversation") as create,
        patch("robothor.crm.dal.send_message", return_value={"id": 100}) as send,
        patch("robothor.audit.logger.log_event"),
        patch("robothor.events.bus.publish") as publish,
    ):
        result = _run()

    resolve.assert_called_once_with(
        "email", "alice@example.com", "Alice Example", tenant_id="tenant-a"
    )
    create.assert_not_called()
    send.assert_called_once_with(
        7, "Alice Example <alice@example.com>: 'Lunch?'", "incoming", tenant_id="tenant-a"
    )
    assert result == {
        "status": "ok",
        "contact": "Alice Example",
        "resolved": True,
        "message_persisted": True,
    }
    stream, event_type, payload = publish.call_args.args
    assert (stream, event_type) == ("crm", "ipc.interaction")
    assert payload["person_id"] == "p-1"
    assert publish.call_args.kwargs == {"source": "bridge", "tenant_id": "tenant-a"}


def test_opens_a_conversation_when_the_contact_has_none() -> None:
    with (
        patch("robothor.crm.dal.resolve_contact", return_value={"person_id": "p-1"}),
        patch("robothor.crm.dal.get_conversations_for_contact", return_value=[]),
        patch("robothor.crm.dal.create_conversation", return_value={"id": 9}) as create,
        patch("robothor.crm.dal.send_message", return_value=None) as send,
        patch("robothor.audit.logger.log_event"),
        patch("robothor.events.bus.publish") as publish,
    ):
        result = _run(source="workspace_ingest")

    create.assert_called_once_with("p-1", tenant_id="tenant-a")
    assert send.call_args.args[0] == 9
    # Accepted but NOT written is reported, never hidden.
    assert result["message_persisted"] is False
    assert publish.call_args.kwargs["source"] == "workspace_ingest"


def test_falls_back_to_the_name_without_an_identifier_and_skips_an_empty_summary() -> None:
    with (
        patch("robothor.crm.dal.resolve_contact", return_value={}) as resolve,
        patch("robothor.crm.dal.send_message") as send,
        patch("robothor.audit.logger.log_event"),
        patch("robothor.events.bus.publish"),
    ):
        result = _run(channel_identifier=None, content_summary="")

    assert resolve.call_args.args[:2] == ("email", "Alice Example")
    send.assert_not_called()
    assert result["resolved"] is False
    assert result["message_persisted"] is None
