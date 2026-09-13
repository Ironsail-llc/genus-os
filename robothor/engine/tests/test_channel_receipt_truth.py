"""No channel may report reach it cannot prove. The adversarial cases.

``test_channel_registry.py`` checks the happy shapes. These are the ones a
hostile review of the channel seam actually found, and each one shipped a
``delivered`` for a send that reached nobody:

* ``delivery._deliver_telegram`` replaced by a bare ``AsyncMock`` — whose
  default return value is a truthy ``MagicMock`` — while the registered sender
  acknowledged nothing. "The call returned something truthy" is one level up
  from "the next line ran", and the module docstring in ``channels/base.py``
  exists to forbid exactly that.
* a replacement that *wraps* the original: the inner call fires POST_DELIVERY
  and so did the outer one, so ``channel_bus`` wrote the operator's briefing
  into main's session twice — the second copy missing its header and its
  platform message ids, which is the reply-to contract.
* a third-party sender that returns its API response object rather than a list
  of messages. ``acknowledged_messages`` falls back to ``list(sent)``, which on
  a mapping yields its keys, so a dict of three fields counted as three
  delivered chunks.
* the event bus: a Redis stream id is not a person having read something, and
  ``DeliveryMode.LOG`` has always left ``delivered_at`` NULL for the same write.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.engine.channels import (
    SendReceipt,
    get_channel,
    register_channel,
    reset_channels,
)
from robothor.engine.channels.sender import SenderChannel
from robothor.engine.delivery import deliver, register_platform_sender, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_channels()
    yield
    reset_channels()
    set_telegram_sender(None)  # type: ignore[arg-type]


@pytest.fixture
def dispatches(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every POST_DELIVERY dispatch, with its payload."""
    calls: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("robothor.engine.delivery._dispatch_post_delivery", _record)
    monkeypatch.setattr(
        "robothor.engine.delivery._persist_delivery_status", AsyncMock(return_value=None)
    )
    return calls


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
        "output_text": "Two invoices need approval.",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


class TestAReplacedDelegateProvesNothingByReturningTrue:
    @pytest.mark.asyncio
    async def test_an_asyncmock_replacement_is_not_a_delivered_send(self, monkeypatch, dispatches):
        """The default ``AsyncMock`` return value is a truthy ``MagicMock``."""
        sender = AsyncMock(return_value=[])  # acknowledges nothing
        set_telegram_sender(sender)
        monkeypatch.setattr("robothor.engine.delivery._deliver_telegram", AsyncMock())
        run = _run()

        result = await deliver(_config(), run)

        assert result is False, "a truthy return value was accepted as proof of delivery"
        assert run.delivery_status != "delivered"
        assert (run.delivery_status or "").startswith("failed:")
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_a_replacement_that_records_nothing_is_unproven(self, monkeypatch, dispatches):
        """The shape `test_run_verification_enforcement` uses: return True, send
        nothing, record nothing."""
        bodies: list[str] = []

        async def _fake(config, text, run):  # noqa: ANN001, ANN202
            bodies.append(text)
            return True

        monkeypatch.setattr("robothor.engine.delivery._deliver_telegram", _fake)
        run = _run()

        result = await deliver(_config(), run)

        assert bodies, "the replacement must still intercept the body"
        assert result is False
        assert run.delivery_status == "failed:telegram_unproven"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_a_replacement_that_records_a_status_is_believed(self, monkeypatch, dispatches):
        """A replacement that stamps the run has supplied evidence."""

        async def _fake(config, text, run):  # noqa: ANN001, ANN202
            run.delivery_status = "partial:2/3"
            run.delivery_channel = "telegram"
            return False

        monkeypatch.setattr("robothor.engine.delivery._deliver_telegram", _fake)
        run = _run()

        result = await deliver(_config(), run)

        assert result is False
        assert run.delivery_status == "partial:2/3"
        assert run.delivered_at is None, "a truncated briefing is not a delivered briefing"


class TestPostDeliveryFiresExactlyOnce:
    @pytest.mark.asyncio
    async def test_one_dispatch_for_a_plain_complete_send(self, dispatches):
        set_telegram_sender(AsyncMock(return_value=[_FakeMessage(101)]))
        run = _run()

        assert await deliver(_config(), run) is True
        assert len(dispatches) == 1
        assert dispatches[0]["platform_message_ids"] == ["101"]
        assert dispatches[0]["chat_id"] == "42"
        assert dispatches[0]["text"].startswith("*Alice*")

    @pytest.mark.asyncio
    async def test_a_wrapping_replacement_does_not_double_dispatch(self, monkeypatch, dispatches):
        """``channel_bus`` writes one chat_messages row per dispatch, so a second
        one is a duplicated turn in the operator's own session."""
        from robothor.engine import delivery as delivery_mod

        set_telegram_sender(AsyncMock(return_value=[_FakeMessage(101)]))
        original = delivery_mod._deliver_telegram
        seen: list[str] = []

        async def _spy(config, text, run):  # noqa: ANN001, ANN202
            seen.append(text)
            return await original(config, text, run)

        monkeypatch.setattr("robothor.engine.delivery._deliver_telegram", _spy)
        run = _run()

        result = await deliver(_config(), run)

        assert seen, "the spy must see the body"
        assert result is True
        assert run.delivery_status == "delivered"
        assert len(dispatches) == 1, (
            f"POST_DELIVERY fired {len(dispatches)} times — channel_bus records each one"
        )
        assert dispatches[0]["platform_message_ids"] == ["101"]
        assert dispatches[0]["text"].startswith("*Alice*")

    @pytest.mark.asyncio
    async def test_nothing_acknowledged_dispatches_nothing(self, dispatches):
        set_telegram_sender(AsyncMock(return_value=[]))
        await deliver(_config(), _run())
        assert dispatches == []


class TestASenderMustReturnMessages:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "returned",
        [
            {"ok": True, "ts": "1.0", "channel": "C1"},
            "ok",
            True,
            42,
        ],
        ids=["api-dict", "string", "bool", "int"],
    )
    async def test_a_non_sequence_return_is_unproven_not_delivered(self, returned):
        """``list()`` of a mapping yields its keys, so an API response object
        counted one delivered chunk per field."""
        from robothor.engine.delivery import apply_receipt

        async def _sender(target: str, text: str, **_: Any) -> Any:
            return returned

        register_platform_sender("acme_chat", _sender)
        run = _run()
        receipt = await SenderChannel("acme_chat").send(
            "room-1", "hello", config=_config(), run=run
        )
        apply_receipt(run, "acme_chat", receipt)

        assert receipt.complete is False
        assert run.delivery_status == "failed:acme_chat_unproven"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_a_list_of_messages_is_believed(self):
        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(7), _FakeMessage(8)]

        register_platform_sender("acme_chat", _sender)
        receipt = await SenderChannel("acme_chat").send(
            "room-1", "hello", config=_config(), run=_run()
        )
        assert receipt.complete is True
        assert receipt.platform_ids == ["7", "8"]


class TestDeliveredAtMeansAPersonHasIt:
    @pytest.mark.asyncio
    async def test_the_event_bus_publishes_without_claiming_delivery(self, monkeypatch, dispatches):
        """The identical publish through ``DeliveryMode.LOG`` leaves
        ``delivered_at`` NULL; naming the bus as a channel must not change that."""
        monkeypatch.setattr("robothor.events.bus.EVENT_BUS_ENABLED", True, raising=False)
        monkeypatch.setattr("robothor.events.bus.publish", lambda **kw: "1-0")
        run = _run()

        result = await deliver(_config(delivery_channel="event_bus"), run)

        assert result is True
        assert run.delivery_status == "published"
        assert run.delivery_channel == "event_bus"
        assert run.delivered_at is None, "a Redis stream id is not a person reading something"
        assert dispatches == [], "there is no platform message for a reply to resolve against"

    @pytest.mark.asyncio
    async def test_a_channel_that_says_delivered_does_set_delivered_at(self, dispatches):
        class _Plain:
            name = "plain"
            inbound_router = None

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def health(self) -> dict[str, Any]:
                return {}

            async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
                return SendReceipt(acknowledged=1, expected=1, platform_ids=["1"], target=target)

        register_channel("plain", _Plain())
        run = _run()
        assert await deliver(_config(delivery_channel="plain"), run) is True
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None


class TestAChannelMayNotClaimReachItCannotProve:
    """``receipt.status`` was used verbatim, which is a hole in the one place
    the design calls "the single mapping from receipt to delivery_status".

    A channel is third-party code — ``deliver()`` already wraps its ``send`` in
    ``try/except`` for exactly that reason — and a channel that returned
    ``status="delivered"`` with nothing acknowledged got ``delivered`` written
    to ``agent_runs``. ``analytics.py`` counts that as reach and
    ``scheduler._maybe_emit_heartbeat_status_ping`` suppresses the fallback
    ping, so a send that arrived nowhere became invisible.
    """

    @staticmethod
    def _liar(status: str, acknowledged: int = 0, expected: int = 3):
        class _Liar:
            name = "liar"
            inbound_router = None

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def health(self) -> dict[str, Any]:
                return {}

            async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
                return SendReceipt(
                    acknowledged=acknowledged, expected=expected, status=status, target=target
                )

        return _Liar()

    @pytest.mark.asyncio
    async def test_delivered_with_nothing_acknowledged_is_refused(self, dispatches):
        register_channel("liar", self._liar("delivered"))
        run = _run()

        result = await deliver(_config(delivery_channel="liar"), run)

        assert result is False
        assert run.delivery_status != "delivered"
        assert (run.delivery_status or "").startswith("failed:")
        assert run.delivered_at is None
        assert dispatches == []

    @pytest.mark.asyncio
    async def test_the_refusal_is_invisible_to_neither_analytics_nor_the_ping(self, dispatches):
        """The two consumers that decide whether anyone is told."""
        register_channel("liar", self._liar("delivered"))
        run = _run()
        await deliver(_config(delivery_channel="liar"), run)

        status = run.delivery_status or ""
        assert status != "delivered", "analytics.py:397 would count this as reach"
        assert not status.startswith("delivered"), "scheduler.py:161 would suppress the ping"

    @pytest.mark.asyncio
    async def test_a_partial_override_is_still_honoured(self, dispatches):
        """Only a claim of REACH is refused; a channel that reports its own
        failure or truncation knows something the counts do not."""
        register_channel("liar", self._liar("partial:1/3", acknowledged=1))
        run = _run()
        await deliver(_config(delivery_channel="liar"), run)
        assert run.delivery_status == "partial:1/3"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_delivered_with_a_complete_receipt_is_believed(self, dispatches):
        register_channel("liar", self._liar("delivered", acknowledged=3, expected=3))
        run = _run()
        assert await deliver(_config(delivery_channel="liar"), run) is True
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None


class TestTheShimCountsTheChunksItSent:
    """``SenderChannel`` hardcoded ``expected = 1``, so a shimmed sender that
    landed 2 of 3 chunks reported ``acknowledged=2 >= expected=1`` → complete →
    ``delivered``. Telegram reports ``partial:2/3`` for identical evidence, which
    is precisely the "one surface ends up stricter than another about the same
    evidence" the shared counter exists to prevent."""

    @pytest.mark.asyncio
    async def test_two_of_three_chunks_is_partial_not_delivered(self):
        from robothor.engine.delivery import apply_receipt

        body = "x" * 9000  # three chunks at a 4000-char limit

        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(11), _FakeMessage(12)]

        register_platform_sender("acme_chat", _sender, chunk_size=4000)
        run = _run()
        channel = get_channel("acme_chat")
        assert channel is not None
        receipt = await channel.send("room-1", body, config=_config(), run=run)
        apply_receipt(run, "acme_chat", receipt)

        assert receipt.expected == 3
        assert receipt.acknowledged == 2
        assert receipt.complete is False
        assert run.delivery_status == "partial:2/3"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_all_three_chunks_is_delivered(self):
        from robothor.engine.delivery import apply_receipt

        body = "x" * 9000

        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(11), _FakeMessage(12), _FakeMessage(13)]

        register_platform_sender("acme_chat", _sender, chunk_size=4000)
        run = _run()
        channel = get_channel("acme_chat")
        assert channel is not None
        receipt = await channel.send("room-1", body, config=_config(), run=run)
        apply_receipt(run, "acme_chat", receipt)

        assert (receipt.acknowledged, receipt.expected) == (3, 3)
        assert run.delivery_status == "delivered"

    @pytest.mark.asyncio
    async def test_a_sender_that_declares_no_chunk_size_still_sends_one_body(self):
        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            return [_FakeMessage(7)]

        register_platform_sender("acme_chat", _sender)
        channel = get_channel("acme_chat")
        assert channel is not None
        receipt = await channel.send("room-1", "hello", config=_config(), run=_run())
        assert (receipt.acknowledged, receipt.expected) == (1, 1)
        assert receipt.complete is True


class TestTheShimGuardsTheTargetLikeTelegramDoes:
    """``failed:telegram_unexpanded_chat_id`` exists because an unexpanded
    ``${VAR}`` shipped once. The shim reproduced none of that."""

    @pytest.mark.asyncio
    async def test_an_unexpanded_variable_is_refused_before_the_send(self):
        calls: list[str] = []

        async def _sender(target: str, text: str, **_: Any) -> list[_FakeMessage]:
            calls.append(target)
            return [_FakeMessage(1)]

        register_platform_sender("acme_chat", _sender)
        channel = get_channel("acme_chat")
        assert channel is not None
        receipt = await channel.send("${ACME_ROOM}", "hello", config=_config(), run=_run())

        assert calls == [], "the send went out to a literal ${ACME_ROOM}"
        assert receipt.status == "failed:acme_chat_unexpanded_target"
        assert receipt.complete is False
