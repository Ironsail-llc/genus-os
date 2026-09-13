"""Who is allowed to answer an ask, and which ask their answer lands on.

The first cut of this feature authorized on the wrong axis. A pending ask
recorded the chat it was sent to and then never looked at it: every answer was
authorized purely by ``_check_owner_gate``, whose default mode is
``chat_id == default_chat_id``. That is wrong in both directions at once —

* the person a question was actually addressed to could not answer it unless
  their chat happened to be the operator's, and
* anyone authorized in the operator's chat could settle an ask registered to a
  completely different chat, because nothing compared the two.

So an ask is now **bound at mint** to ``(chat_id, addressee sender_id)``, and an
answer settles it only when it comes from that chat AND that sender. The owner
gate did not go away: it remains an *additional* requirement for an ask raised
in the operator's own chat, and the only authorization for an ask with no
addressee at all (every permission escalation, which is raised for the operator
by construction).

Every refusal here is silent to the forger and counted in the log. Telling an
unauthorized caller which of the three checks they failed is telling them how to
pass it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.channels import telegram_ask

OPERATOR = "12345"  # engine_config.default_chat_id
OTHER_CHAT = "555555"
ADDRESSEE = "777"
INTRUDER = "888"


@pytest.fixture(autouse=True)
def _clean():
    telegram_ask.reset_pending_asks()
    yield
    telegram_ask.reset_pending_asks()


@pytest.fixture
def bot(engine_config):
    from robothor.engine.telegram import TelegramBot

    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        raw = MagicMock()
        raw.send_message = AsyncMock()
        mock_bot_cls.return_value = raw
        instance = TelegramBot(engine_config, MagicMock())
        instance.bot = raw
        instance._enqueue_message = AsyncMock()
        instance._resolve_user = MagicMock(
            return_value={
                "tenant_id": "test-tenant",
                "display_name": "Someone",
                "role": "user",
                "user_id": "tu-1",
            }
        )
        yield instance


def _mint(chat_id: str, sender_id: str, *, options=(), delivered_as="text", message_id=""):
    return telegram_ask.register_ask(
        chat_id=chat_id,
        sender_id=sender_id,
        options=tuple(options),
        delivered_as=delivered_as,
        message_id=message_id,
    )


def _callback(data: str, *, chat_id: str, user_id: str):
    callback = MagicMock()
    callback.data = data
    callback.message = MagicMock()
    callback.message.chat.id = chat_id
    callback.message.edit_reply_markup = AsyncMock()
    callback.from_user = MagicMock(id=user_id)
    callback.answer = AsyncMock()
    return callback


def _message(chat_id: str, text: str, *, user_id: str, reply_to: str = ""):
    message = MagicMock()
    message.text = text
    message.chat.id = chat_id
    message.chat.type = "private"
    message.from_user.id = user_id
    message.from_user.username = None
    message.from_user.first_name = "Someone"
    message.message_id = 7
    if reply_to:
        message.reply_to_message = MagicMock(message_id=reply_to)
    else:
        message.reply_to_message = None
    return message


# ─── Keyboard answers ───────────────────────────────────────────────


class TestTheAddresseeCanAnswer:
    @pytest.mark.asyncio
    async def test_the_addressee_answers_the_buttons_in_their_own_chat(self, bot):
        """The bug this replaces: a registered non-owner ran an agent from their
        own chat, was asked a question there, tapped a button, and was told
        Unauthorized. ``ask_user`` worked in exactly one chat per instance."""
        ask_id, future = _mint(OTHER_CHAT, ADDRESSEE, options=("A", "B"), delivered_as="keyboard")

        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:1", chat_id=OTHER_CHAT, user_id=ADDRESSEE)
        )

        assert future.result() == "B"

    @pytest.mark.asyncio
    async def test_the_operator_answers_their_own_ask(self, bot):
        ask_id, future = _mint(OPERATOR, "4242", options=("A", "B"), delivered_as="keyboard")

        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:0", chat_id=OPERATOR, user_id="4242")
        )

        assert future.result() == "A"


class TestForgedAnswersAreIgnored:
    @pytest.mark.asyncio
    async def test_a_callback_from_another_chat_cannot_settle_an_ask(self, bot):
        """The ask id travels in ``callback_data``. Without the chat check,
        anyone authorized in the operator's chat settles any ask in the
        process."""
        ask_id, future = _mint(OTHER_CHAT, ADDRESSEE, options=("A", "B"), delivered_as="keyboard")

        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:0", chat_id=OPERATOR, user_id="4242")
        )

        assert not future.done()
        assert ask_id in telegram_ask.pending_ask_ids()

    @pytest.mark.asyncio
    async def test_a_different_sender_in_the_bound_chat_cannot_settle_it(self, bot):
        ask_id, future = _mint(OTHER_CHAT, ADDRESSEE, options=("A", "B"), delivered_as="keyboard")

        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:0", chat_id=OTHER_CHAT, user_id=INTRUDER)
        )

        assert not future.done()

    @pytest.mark.asyncio
    async def test_an_ask_in_the_operator_chat_still_needs_the_owner_gate(self, bot):
        """Belt and braces. The binding alone would already refuse a different
        sender; the gate is kept so the operator's chat keeps the exact
        authorization ladder ``ROBOTHOR_TELEGRAM_ROLE_GATES`` governs."""
        ask_id, future = _mint(OPERATOR, "4242", options=("A", "B"), delivered_as="keyboard")
        bot._check_owner_gate = MagicMock(return_value=False)

        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:0", chat_id=OPERATOR, user_id="4242")
        )

        assert not future.done()

    @pytest.mark.asyncio
    async def test_an_unaddressed_ask_is_owner_gated(self, bot):
        """Every permission escalation is raised for the operator and carries no
        addressee. With nobody bound, the owner gate is the only authorization
        there is — so it must be required, not skipped."""
        ask_id, future = _mint(OPERATOR, "", options=("Approve", "Deny"), delivered_as="keyboard")
        bot._check_owner_gate = MagicMock(return_value=False)

        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:0", chat_id=OPERATOR, user_id="4242")
        )
        assert not future.done()

        bot._check_owner_gate = MagicMock(return_value=True)
        await telegram_ask.handle_ask_callback(
            bot, _callback(f"ask:{ask_id}:0", chat_id=OPERATOR, user_id="4242")
        )
        assert future.result() == "Approve"

    @pytest.mark.asyncio
    async def test_a_refusal_says_nothing_about_which_check_failed(self, bot):
        ask_id, _ = _mint(OTHER_CHAT, ADDRESSEE, options=("A",), delivered_as="keyboard")
        callback = _callback(f"ask:{ask_id}:0", chat_id=OPERATOR, user_id="4242")

        await telegram_ask.handle_ask_callback(bot, callback)

        answered = " ".join(str(a) for a in callback.answer.call_args.args)
        assert OTHER_CHAT not in answered
        assert ADDRESSEE not in answered


# ─── Free-text answers ──────────────────────────────────────────────


class TestFreeTextIsBoundToo:
    @pytest.mark.asyncio
    async def test_the_addressee_answers_in_their_own_chat(self, bot):
        _, future = _mint(OTHER_CHAT, ADDRESSEE)

        await bot.handle_text(_message(OTHER_CHAT, "Tuesday", user_id=ADDRESSEE))

        assert future.result() == "Tuesday"
        bot._enqueue_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_non_owner_in_the_operators_chat_does_not_answer_it(self, bot):
        """I1. Under the default gate mode ``_check_owner_gate`` is chat
        equality only, so anyone who can post in the operator's chat — a group
        member, a second registered user, a shared device — used to supply the
        answer the agent then acted on. The binding is what refuses them, and it
        refuses them regardless of ``ROBOTHOR_TELEGRAM_ROLE_GATES``."""
        _, future = _mint(OPERATOR, "4242")

        await bot.handle_text(_message(OPERATOR, "delete it", user_id="5555"))

        assert not future.done()
        bot._enqueue_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_reply_in_another_chat_does_not_answer_it(self, bot):
        _, future = _mint(OTHER_CHAT, ADDRESSEE)

        await bot.handle_text(_message(OPERATOR, "Tuesday", user_id="4242"))

        assert not future.done()
        bot._enqueue_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_ordinary_text_with_no_pending_ask_reaches_the_run(self, bot):
        await bot.handle_text(_message(OPERATOR, "what is the status", user_id="4242"))

        bot._enqueue_message.assert_called_once()


class TestTheNumberedFallbackIsAnswerable:
    """When no aiogram ``Bot`` is reachable the options go out as numbered
    text. Before this, ``has_pending_ask`` skipped every ask with options, so
    the operator typed "1" and it was enqueued to the run — the ask could only
    ever time out, and for an escalation that is a silent deny."""

    @pytest.mark.asyncio
    async def test_a_numbered_reply_answers_it(self, bot):
        _, future = _mint(OTHER_CHAT, ADDRESSEE, options=("Acme", "Globex"))

        await bot.handle_text(_message(OTHER_CHAT, "2", user_id=ADDRESSEE))

        assert future.result() == "Globex"
        bot._enqueue_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_exact_option_text_answers_it(self, bot):
        _, future = _mint(OTHER_CHAT, ADDRESSEE, options=("Acme", "Globex"))

        await bot.handle_text(_message(OTHER_CHAT, "Globex", user_id=ADDRESSEE))

        assert future.result() == "Globex"

    @pytest.mark.asyncio
    async def test_an_out_of_range_number_is_not_an_answer(self, bot):
        _, future = _mint(OTHER_CHAT, ADDRESSEE, options=("Acme", "Globex"))

        await bot.handle_text(_message(OTHER_CHAT, "9", user_id=ADDRESSEE))

        assert not future.done()
        bot._enqueue_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_keyboard_ask_ignores_free_text(self, bot):
        """A question offered as buttons is answered by tapping one. Treating
        the next line typed as its answer would turn "hang on, what was the
        second option again?" into a decision."""
        _, future = _mint(
            OTHER_CHAT, ADDRESSEE, options=("Acme", "Globex"), delivered_as="keyboard"
        )

        await bot.handle_text(_message(OTHER_CHAT, "Globex", user_id=ADDRESSEE))

        assert not future.done()
        bot._enqueue_message.assert_called_once()


class TestTwoAsksInOneChat:
    @pytest.mark.asyncio
    async def test_a_quoted_reply_answers_the_ask_it_quotes(self, bot):
        """Telegram already tells us which message the operator replied to, and
        an ask knows its own message id. Without this the second question's
        answer lands on the first."""
        _, first = _mint(OTHER_CHAT, ADDRESSEE, message_id="101")
        _, second = _mint(OTHER_CHAT, ADDRESSEE, message_id="102")

        await bot.handle_text(
            _message(OTHER_CHAT, "the second one", user_id=ADDRESSEE, reply_to="102")
        )

        assert not first.done()
        assert second.result() == "the second one"

    @pytest.mark.asyncio
    async def test_without_a_quote_the_oldest_wins(self, bot):
        """Documented rather than clever: with nothing to correlate on, the
        question that has been waiting longest is the one being answered."""
        _, first = _mint(OTHER_CHAT, ADDRESSEE, message_id="101")
        _, second = _mint(OTHER_CHAT, ADDRESSEE, message_id="102")

        await bot.handle_text(_message(OTHER_CHAT, "Tuesday", user_id=ADDRESSEE))

        assert first.result() == "Tuesday"
        assert not second.done()

    @pytest.mark.asyncio
    async def test_a_quote_of_someone_elses_ask_is_not_an_answer(self, bot):
        _, other = _mint(OPERATOR, "4242", message_id="101")

        await bot.handle_text(_message(OTHER_CHAT, "Tuesday", user_id=ADDRESSEE, reply_to="101"))

        assert not other.done()
        bot._enqueue_message.assert_called_once()


class TestTheChannelBindsWhatItMints:
    @pytest.mark.asyncio
    async def test_ask_records_the_addressee_it_was_given(self):
        """The binding is only as good as the mint. ``TelegramChannel.ask`` must
        carry the addressee onto the pending record, or every check above is
        comparing against an empty string."""
        from robothor.engine import delivery
        from robothor.engine.channels.telegram import TelegramChannel

        sent: list = []

        async def sender(chat_id, text, **kw):
            sent.append((chat_id, text))
            return [MagicMock(message_id=55)]

        previous = delivery.get_platform_sender("telegram")
        delivery.register_platform_sender("telegram", sender)
        try:
            task = asyncio.create_task(
                TelegramChannel().ask("When?", timeout=5.0, target=OTHER_CHAT, addressee=ADDRESSEE)
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            ask_id = telegram_ask.pending_ask_ids()[0]
            pending = telegram_ask._pending_asks[ask_id]
            assert pending.chat_id == OTHER_CHAT
            assert pending.sender_id == ADDRESSEE
            assert pending.message_id == "55"

            assert (
                telegram_ask.resolve_ask_choice(
                    ask_id, 0, chat_id=OTHER_CHAT, sender_id=ADDRESSEE, owner_ok=False
                )
                is None
            )  # no options: nothing to choose
            pending.future.set_result("Tuesday")
            assert await task == "Tuesday"
        finally:
            if previous is None:
                delivery._platform_senders.pop("telegram", None)
            else:
                delivery.register_platform_sender("telegram", previous)
