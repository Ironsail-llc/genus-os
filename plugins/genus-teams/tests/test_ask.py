"""Asking one person a question in a room where everyone can press the button.

An Adaptive Card in a Teams channel is visible to every member of that channel,
and every one of them can press its buttons. On Telegram the equivalent needed a
deliberate attack; here it is a Tuesday. So the binding
``robothor/engine/channels/telegram_ask.py`` established — an ask belongs to a
conversation AND to a person — is not a precaution copied out of habit, it is
the only thing that makes a card safe to send at all.

The other rule under test is the platform's: ``None`` is the only non-answer. A
timeout must never return one of the options, because an option returned because
the clock ran out is an approval nobody gave.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from genus_teams import ask as ask_module
from genus_teams.channel import TeamsChannel

CONVERSATION = "19:meeting_abc@thread.v2"
ALICE = "aaaaaaaa-0000-0000-0000-00000000aaaa"
BOB = "bbbbbbbb-0000-0000-0000-00000000bbbb"


class _Channel:
    """A channel that records cards instead of sending them."""

    name = "teams"
    tenant_id = "00000000-0000-0000-0000-000000000000"

    def __init__(self, *, delivers: bool = True, conversation: str = CONVERSATION) -> None:
        self.cards: list[dict[str, Any]] = []
        self._delivers = delivers
        self._conversation = conversation

    async def reference_for(self, target: str) -> Any:
        from robothor.engine.channels.conversations import ConversationRef

        if not self._conversation:
            return None
        return ConversationRef(
            channel="teams",
            native_id=target,
            conversation_id=self._conversation,
            service_url="https://smba.trafficmanager.net/emea/",
        )

    async def send_card(self, target: str, card: dict[str, Any]) -> bool:
        self.cards.append(card)
        return self._delivers


@pytest.fixture(autouse=True)
def _clean():
    ask_module.reset_pending_asks()
    yield
    ask_module.reset_pending_asks()


def _submit(ask_id: str, **payload: Any) -> dict[str, Any]:
    return {"type": "message", "value": {ask_module.ASK_FIELD: ask_id, **payload}}


async def _ask(channel: _Channel, **kw: Any) -> str | None:
    return await ask_module.ask_over_card(
        channel,
        kw.pop("question", "Ship it?"),
        kw.pop("options", ("yes", "no")),
        timeout=kw.pop("timeout", 5.0),
        target=kw.pop("target", ALICE),
        addressee=kw.pop("addressee", ALICE),
    )


class TestTheCard:
    @pytest.mark.asyncio
    async def test_each_option_is_a_button_carrying_its_index(self):
        channel = _Channel()
        task = asyncio.create_task(_ask(channel, options=("ship", "hold")))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        card = channel.cards[0]
        titles = [action["title"] for action in card["actions"]]
        assert titles == ["ship", "hold"]
        choices = [action["data"][ask_module.CHOICE_FIELD] for action in card["actions"]]
        assert choices == [0, 1], (
            "the option TEXT travelled instead of its index; a relabelled button "
            "would then answer with something that was never offered"
        )

        ask_id = card["actions"][0]["data"][ask_module.ASK_FIELD]
        ask_module.settle_from_activity(
            _submit(ask_id, **{ask_module.CHOICE_FIELD: 0}),
            conversation_id=CONVERSATION,
            native_id=ALICE,
        )
        assert await task == "ship"

    @pytest.mark.asyncio
    async def test_a_question_with_no_options_offers_a_text_input(self):
        channel = _Channel()
        task = asyncio.create_task(_ask(channel, options=()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        card = channel.cards[0]
        inputs = [item for item in card["body"] if item["type"] == "Input.Text"]
        assert inputs and inputs[0]["id"] == ask_module.TEXT_FIELD

        ask_id = card["actions"][0]["data"][ask_module.ASK_FIELD]
        ask_module.settle_from_activity(
            _submit(ask_id, **{ask_module.TEXT_FIELD: "  next Tuesday  "}),
            conversation_id=CONVERSATION,
            native_id=ALICE,
        )
        assert await task == "next Tuesday"

    @pytest.mark.asyncio
    async def test_more_options_than_a_card_can_show_are_capped(self):
        channel = _Channel()
        task = asyncio.create_task(_ask(channel, options=tuple(f"o{i}" for i in range(20))))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(channel.cards[0]["actions"]) == ask_module.MAX_ASK_OPTIONS
        task.cancel()


class TestOnlyTheAddresseeMayAnswer:
    @pytest.mark.asyncio
    async def test_an_answer_from_somebody_else_is_refused_and_the_ask_keeps_waiting(self):
        channel = _Channel()
        task = asyncio.create_task(_ask(channel, addressee=ALICE))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ask_id = channel.cards[0]["actions"][0]["data"][ask_module.ASK_FIELD]

        handled = ask_module.settle_from_activity(
            _submit(ask_id, **{ask_module.CHOICE_FIELD: 0}),
            conversation_id=CONVERSATION,
            native_id=BOB,
        )
        assert handled is True, "the card submit was handed to the agent as a message"
        assert not task.done(), "Bob answered a question asked of Alice"

        ask_module.settle_from_activity(
            _submit(ask_id, **{ask_module.CHOICE_FIELD: 1}),
            conversation_id=CONVERSATION,
            native_id=ALICE,
        )
        assert await task == "no"

    @pytest.mark.asyncio
    async def test_an_answer_from_another_conversation_is_refused(self):
        """The same person, the same ask id, a different conversation: a card's
        payload can be replayed anywhere the bot is installed."""
        channel = _Channel()
        task = asyncio.create_task(_ask(channel))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ask_id = channel.cards[0]["actions"][0]["data"][ask_module.ASK_FIELD]

        ask_module.settle_from_activity(
            _submit(ask_id, **{ask_module.CHOICE_FIELD: 0}),
            conversation_id="19:somewhere-else@thread.v2",
            native_id=ALICE,
        )
        assert not task.done()
        task.cancel()

    @pytest.mark.asyncio
    async def test_an_index_outside_the_options_answers_nothing(self):
        channel = _Channel()
        task = asyncio.create_task(_ask(channel, options=("yes", "no")))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ask_id = channel.cards[0]["actions"][0]["data"][ask_module.ASK_FIELD]

        ask_module.settle_from_activity(
            _submit(ask_id, **{ask_module.CHOICE_FIELD: 99}),
            conversation_id=CONVERSATION,
            native_id=ALICE,
        )
        assert not task.done()
        task.cancel()


class TestNoneIsTheOnlyNonAnswer:
    @pytest.mark.asyncio
    async def test_a_timeout_is_none_and_never_an_option(self):
        channel = _Channel()
        assert await _ask(channel, timeout=0.01) is None

    @pytest.mark.asyncio
    async def test_a_card_that_never_went_out_is_not_waited_on(self):
        """Waiting five minutes on a question nobody received is worse than
        saying immediately that nobody was asked."""
        channel = _Channel(delivers=False)
        assert await _ask(channel, timeout=30.0) is None

    @pytest.mark.asyncio
    async def test_a_target_with_no_conversation_is_none(self):
        channel = _Channel(conversation="")
        assert await _ask(channel, timeout=30.0) is None

    @pytest.mark.asyncio
    async def test_nothing_is_left_pending_afterwards(self):
        channel = _Channel()
        await _ask(channel, timeout=0.01)
        assert ask_module.pending_ask_ids() == []


class TestAnActivityThatIsNotAnAnswer:
    def test_an_ordinary_message_is_not_treated_as_a_submit(self):
        assert (
            ask_module.settle_from_activity(
                {"type": "message", "text": "hello"},
                conversation_id=CONVERSATION,
                native_id=ALICE,
            )
            is False
        )

    def test_a_submit_for_an_unknown_ask_is_swallowed_rather_than_run_as_a_message(self):
        """A button pressed after the tool gave up. Handing ``{"genus_ask": …}``
        to the agent as a sentence would be worse than dropping it."""
        assert (
            ask_module.settle_from_activity(
                _submit("not-a-live-ask"), conversation_id=CONVERSATION, native_id=ALICE
            )
            is True
        )


class TestTheChannelRefusesToAskWhenItCannotHear:
    @pytest.mark.asyncio
    async def test_ask_raises_when_no_endpoint_is_mounted(self, monkeypatch):
        """``NoListenerError``, not ``None``: the callers act on the difference.
        ``None`` means the person did not reply; this means nobody could have,
        so ``ask_user`` records a durable question instead of waiting."""
        from robothor.engine.channels.base import NoListenerError

        channel = TeamsChannel()
        monkeypatch.setattr(type(channel), "inbound_router", property(lambda self: None))
        with pytest.raises(NoListenerError):
            await channel.ask("Ship it?", ("yes", "no"), target=ALICE, addressee=ALICE)
