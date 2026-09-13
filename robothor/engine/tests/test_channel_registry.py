"""The channel protocol and registry: receipts, built-ins, and the sender shim.

One rule runs through every test here, and it is the rule ``alerts.py`` learned
the hard way after 432+ pages went nowhere while the log said "sent": **a
receipt is derived from what the sender returned, never from the fact that the
next line ran.** ``TelegramBot.send_message`` swallows per-chunk exceptions and
returns one entry per chunk that actually landed — an empty list when none did —
so the returned value is the only evidence of delivery.

``engine/slack.py`` is the live counter-example the shim has to survive: its
``slack_send`` returns ``None``, so a channel built from it must report zero
acknowledged and fail loudly rather than inherit Telegram's "no news is good
news".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.channels import (
    BUILTIN_CHANNELS,
    Channel,
    SendReceipt,
    get_channel,
    list_channels,
    receipt_from,
    register_channel,
    reset_channels,
)
from robothor.engine.channels.telegram import TelegramChannel
from robothor.engine.chunking import split_telegram_message
from robothor.engine.delivery import get_platform_sender, register_platform_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_channels()
    yield
    reset_channels()


def _config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "alice",
        "name": "Alice",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "42",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


def _run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "id": "run-1",
        "agent_id": "alice",
        "status": RunStatus.COMPLETED,
        "output_text": "body",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _three_chunk_body(config: AgentConfig) -> str:
    body = "x" * (4096 * 2 + 100)
    assert len(split_telegram_message(f"*{config.name}*\n\n{body}")) == 3
    return body


class TestTheReceipt:
    def test_a_complete_receipt_is_complete(self):
        assert SendReceipt(acknowledged=3, expected=3).complete is True

    def test_zero_acknowledged_is_never_complete(self):
        """``expected == 0`` must not make "nothing sent" look like success."""
        assert SendReceipt(acknowledged=0, expected=0).complete is False

    def test_receipt_from_counts_the_senders_return_value(self):
        receipt = receipt_from([_FakeMessage(11), _FakeMessage(12)], expected=3)
        assert (receipt.acknowledged, receipt.expected) == (2, 3)
        assert receipt.platform_ids == ["11", "12"]
        assert receipt.complete is False

    def test_receipt_from_none_is_zero(self):
        assert receipt_from(None, expected=1).acknowledged == 0


class TestTelegramWrapper:
    @pytest.mark.asyncio
    async def test_telegram_wrapper_receipt_from_sender_return(self, monkeypatch):
        config = _config()
        body = _three_chunk_body(config)
        sent = [_FakeMessage(11), _FakeMessage(12), _FakeMessage(13)]

        async def _sender(chat_id: str, text: str, **_: Any) -> list[_FakeMessage]:
            return sent

        register_platform_sender("telegram", _sender)
        receipt = await TelegramChannel().send("42", body, config=config, run=_run())

        assert (receipt.acknowledged, receipt.expected) == (3, 3)
        assert receipt.complete is True
        assert receipt.platform_ids == ["11", "12", "13"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("returned", [[], None], ids=["empty-list", "none"])
    async def test_a_sender_that_acknowledges_nothing_is_not_delivered(self, returned):
        """``engine/slack.py``'s ``slack_send`` returns ``None`` today, and
        ``TelegramBot.send_message`` returns ``[]`` when every chunk failed.
        Both mean the operator saw nothing."""
        from robothor.engine.delivery import apply_receipt

        async def _sender(chat_id: str, text: str, **_: Any) -> Any:
            return returned

        register_platform_sender("telegram", _sender)
        run = _run()
        receipt = await TelegramChannel().send("42", "hello", config=_config(), run=run)
        assert apply_receipt(run, "telegram", receipt) is False

        assert receipt.acknowledged == 0
        assert receipt.complete is False
        assert run.delivery_status == "failed:telegram_send"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_partial_receipt_is_not_delivered(self):
        from robothor.engine.delivery import apply_receipt

        config = _config()
        body = _three_chunk_body(config)

        async def _sender(chat_id: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(11)]

        register_platform_sender("telegram", _sender)
        run = _run()
        receipt = await TelegramChannel().send("42", body, config=config, run=run)
        assert apply_receipt(run, "telegram", receipt) is False

        assert receipt.complete is False
        assert run.delivery_status == "partial:1/3"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_a_missing_sender_is_a_failure_not_an_exception(self):
        register_platform_sender("telegram", None)  # type: ignore[arg-type]
        receipt = await TelegramChannel().send("42", "hello", config=_config(), run=_run())
        assert receipt.status == "failed:telegram_no_sender"

    @pytest.mark.asyncio
    async def test_resolve_identity_answers_none_for_an_unknown_sender(self):
        """Implemented in C8, and still never returns a plausible default.

        ``ask`` and ``resolve_identity`` were both asserted here as refusals
        while they were unimplemented. Both are now real, and both keep the
        property that made the refusal correct: an unknown sender resolves to
        ``None``, not to a fabricated identity. The delegation itself is
        asserted rather than the database, because an implementation that
        stopped calling the resolver would pass any test that only checked the
        return value."""
        channel = TelegramChannel()
        with patch("robothor.identity.resolvers.resolve_identity", return_value=None) as resolver:
            assert await channel.resolve_identity("42") is None
        resolver.assert_called_once()

    @pytest.mark.asyncio
    async def test_ask_with_no_target_is_none_and_not_an_option(self):
        assert await TelegramChannel().ask("Approve?", ["yes", "no"], timeout=1.0) is None


class TestTheRegistry:
    def test_builtin_names_are_registered(self):
        """Enumerated, not derived, so adding one is a visible decision.

        ``slack`` joined the set when the outbound channel stopped depending on
        the inbound Socket Mode bot having started: a non-built-in name gets a
        ``SenderChannel`` wrapped around whatever ``SlackBot.start()``
        registered, which would put the shim back in front of the real channel.

        ``webchat`` joined it with C9. It writes into a member's own
        ``chat_sessions`` rows and inbox, so a plugin able to claim the name
        could redirect every member-facing delivery while the session it wrote
        to still looked like the member's.

        ``email`` joined it with C11 on that same footing: a plugin able to
        claim the name would become the surface the ``do_not_contact`` guard
        runs inside, and an opt-out control a package can replace merely by
        being installed is not a control.
        """
        assert frozenset({"telegram", "event_bus", "slack", "webchat", "email"}) == BUILTIN_CHANNELS
        for name in BUILTIN_CHANNELS:
            assert get_channel(name) is not None, f"built-in channel {name!r} is missing"

    def test_a_builtin_is_listed(self):
        assert set(BUILTIN_CHANNELS) <= set(list_channels())

    def test_an_unregistered_name_is_none(self):
        assert get_channel("teams") is None

    def test_a_blank_name_is_none(self):
        assert get_channel("") is None

    def test_a_registered_channel_satisfies_the_protocol(self):
        assert isinstance(get_channel("telegram"), Channel)

    def test_a_plugin_may_not_be_registered_over_a_builtin(self):
        class _Impostor:
            name = "telegram"
            inbound_router = None

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def health(self) -> dict[str, Any]:
                return {}

            async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
                return SendReceipt(acknowledged=1, expected=1)

        with pytest.raises(ValueError, match="telegram"):
            register_channel("telegram", _Impostor())
        assert isinstance(get_channel("telegram"), TelegramChannel)


class TestTheSenderShim:
    @pytest.mark.asyncio
    async def test_register_platform_sender_shim_registers_a_channel(self):
        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(7)]

        register_platform_sender("acme_chat", _sender)

        assert get_platform_sender("acme_chat") is _sender
        channel = get_channel("acme_chat")
        assert channel is not None
        assert isinstance(channel, Channel)
        receipt = await channel.send("room-1", "hello", config=_config(), run=_run())
        assert isinstance(receipt, SendReceipt)
        assert receipt.complete is True
        assert receipt.platform_ids == ["7"]

    @pytest.mark.asyncio
    async def test_a_shimmed_sender_returning_none_reports_zero(self):
        """A sender offering no evidence. Truthfully failed beats a lie.

        Written against ``slack`` when ``slack_send`` returned ``None``; the
        name moved to a non-built-in because ``slack`` now has a real channel
        of its own and the shim no longer wraps it. The property under test is
        the shim's, not Slack's.
        """

        async def _returns_nothing(target: str, text: str, **_: Any) -> None:
            return None

        register_platform_sender("acme_chat", _returns_nothing)
        channel = get_channel("acme_chat")
        assert channel is not None
        receipt = await channel.send("room-1", "hello", config=_config(), run=_run())
        assert receipt.acknowledged == 0
        assert receipt.complete is False

    def test_the_shim_does_not_displace_the_builtin_telegram_channel(self):
        """``TelegramBot.__init__`` calls ``set_telegram_sender`` as a side
        effect; that must not swap the built-in wrapper for a bare sender."""

        async def _sender(target: str, text: str, **_: Any) -> list[Any]:
            return []

        register_platform_sender("telegram", _sender)
        assert isinstance(get_channel("telegram"), TelegramChannel)


class TestTheRecursionHazardIsEnforced:
    """``_deliver_telegram``'s docstring warns that a replacement must not route
    back through ``get_channel("telegram").send`` — ``send`` would see itself
    replaced, call the replacement, and go round forever. A documented hazard
    whose only symptom is a hung delivery is not a guard."""

    @pytest.mark.asyncio
    async def test_a_replacement_that_routes_back_through_send_is_refused(self, monkeypatch):
        from robothor.engine.channels.telegram import TelegramChannel

        async def _loops_forever(config, text, run):  # noqa: ANN001, ANN202
            channel = get_channel("telegram")
            assert channel is not None
            await channel.send(config.delivery_to, text, config=config, run=run)
            return True

        monkeypatch.setattr("robothor.engine.delivery._deliver_telegram", _loops_forever)

        async def _sender(chat_id: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(1)]

        register_platform_sender("telegram", _sender)
        run = _run()

        with pytest.raises(RuntimeError, match="routes back through") as excinfo:
            await TelegramChannel().send("42", "hello", config=_config(), run=run)

        # RecursionError IS a RuntimeError and its message contains "recursion",
        # so a loose assertion here passes on the unguarded code. The point of
        # the guard is a diagnosable error instead of a blown stack.
        assert not isinstance(excinfo.value, RecursionError), (
            "the hazard is still only caught by the interpreter running out of stack"
        )

    @pytest.mark.asyncio
    async def test_two_ordinary_sends_in_a_row_are_not_mistaken_for_recursion(self):
        from robothor.engine.channels.telegram import TelegramChannel

        async def _sender(chat_id: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(1)]

        register_platform_sender("telegram", _sender)
        channel = TelegramChannel()
        assert (await channel.send("42", "one", config=_config(), run=_run())).complete
        assert (await channel.send("42", "two", config=_config(), run=_run())).complete

    @pytest.mark.asyncio
    async def test_concurrent_sends_do_not_see_each_other(self):
        """The flag must be per-task, or two agents delivering at once would
        report a recursion that never happened."""
        import asyncio

        from robothor.engine.channels.telegram import TelegramChannel

        async def _slow_sender(chat_id: str, text: str, **_: Any) -> list[_FakeMessage]:
            await asyncio.sleep(0)
            return [_FakeMessage(1)]

        register_platform_sender("telegram", _slow_sender)
        channel = TelegramChannel()
        receipts = await asyncio.gather(
            *(channel.send("42", f"body {i}", config=_config(), run=_run()) for i in range(5))
        )
        assert all(r.complete for r in receipts)


class TestResetRebuildsTheSenderShims:
    """``reset_channels()`` cleared the registry but not ``_platform_senders``,
    and ``register_platform_sender("slack", ...)`` is called once, at
    ``SlackBot.start()``. So a reset made the ``slack`` channel unreachable until
    the next bot start — which for a running engine is never."""

    @pytest.mark.asyncio
    async def test_a_registered_sender_survives_a_reset(self):
        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(9)]

        register_platform_sender("acme_chat", _sender)
        assert get_channel("acme_chat") is not None

        reset_channels()

        channel = get_channel("acme_chat")
        assert channel is not None, "the shim vanished and nothing will re-register it"
        receipt = await channel.send("room-1", "hello", config=_config(), run=_run())
        assert receipt.complete is True

    def test_a_rebuilt_shim_keeps_its_declared_chunk_size(self):
        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(9)]

        register_platform_sender("acme_chat", _sender, chunk_size=4000)
        reset_channels()
        channel = get_channel("acme_chat")
        assert channel is not None
        assert getattr(channel, "chunk_size", None) == 4000
