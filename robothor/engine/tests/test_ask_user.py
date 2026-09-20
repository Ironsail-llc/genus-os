"""`ask_user`, `Channel.ask` on Telegram, and the run-status sink.

The failure this feature exists to avoid is an agent that guesses. The failure
THIS file exists to avoid is an ask that looks answered when nobody answered
it: every test below either proves an answer came from a person, or proves the
tool said so when one did not.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import run_status
from robothor.engine.channels.telegram import TelegramChannel
from robothor.engine.tools.dispatch import ToolContext

ASK_USER = "ask_user"


def _handler():
    from robothor.engine.tools.handlers.ask_user import HANDLERS

    return HANDLERS[ASK_USER]


# ─── Fakes ──────────────────────────────────────────────────────────


class _FakeRawBot:
    """Stands in for the aiogram ``Bot`` behind ``TelegramBot``."""

    def __init__(self) -> None:
        self.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))


class _FakeTelegramBot:
    """Stands in for ``TelegramBot``: a wrapper whose ``.bot`` is the real one.

    ``TelegramChannel.ask`` has to reach the raw bot to attach an inline
    keyboard, exactly as ``permission_escalation._send_prompt`` does — the
    wrapper's own ``send_message`` drops ``reply_markup``.
    """

    def __init__(self) -> None:
        self.bot = _FakeRawBot()
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), str(text)))
        return [SimpleNamespace(message_id=99)]


@pytest.fixture
def telegram_sender():
    """Register a Telegram platform sender and tear it down afterwards."""
    from robothor.engine import delivery

    wrapper = _FakeTelegramBot()
    previous = delivery.get_platform_sender("telegram")
    delivery.register_platform_sender("telegram", wrapper.send_message)
    yield wrapper
    if previous is None:
        delivery._platform_senders.pop("telegram", None)
    else:
        delivery.register_platform_sender("telegram", previous)


@pytest.fixture(autouse=True)
def _clean_ask_registry():
    from robothor.engine.channels import telegram_ask as tg_channel

    tg_channel.reset_pending_asks()
    yield
    tg_channel.reset_pending_asks()


@pytest.fixture(autouse=True)
def _clean_status_sinks():
    run_status.reset_status_sinks()
    yield
    run_status.reset_status_sinks()


class _RecordingChannel:
    """A channel that answers with whatever the test told it to."""

    name = "recording"
    inbound_router = None

    def __init__(self, answer: str | None = None, raises: BaseException | None = None) -> None:
        self._answer = answer
        self._raises = raises
        self.calls: list[dict] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {"channel": self.name}

    async def send(self, target, text, **kw):
        raise AssertionError("ask_user must not deliver through send()")

    async def ask(self, question, options=(), *, timeout=300.0, target="", addressee=""):
        self.calls.append(
            {
                "question": question,
                "options": list(options),
                "timeout": timeout,
                "target": target,
                "addressee": addressee,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._answer

    async def resolve_identity(self, native_id):
        raise NotImplementedError


#: A member's ``user_accounts.id`` — what a webchat ask is aimed at.
WEBCHAT_USER = "44444444-4444-4444-8444-444444444444"


class _RowIdChannel(_RecordingChannel):
    """A channel that declares it wants the durable row's id, as webchat does.

    The flag is the whole mechanism: ``_ask_channel`` probes for it and passes
    the extra kwargs only to a channel that asked for them, which is why adding
    webchat's needs edited neither Telegram nor Slack.
    """

    name = "webchat"
    ask_wants_question_id = True

    async def ask(  # type: ignore[override]
        self, question, options=(), *, timeout=300.0, target="", addressee="", **kw
    ):
        self.calls.append(
            {
                "question": question,
                "options": list(options),
                "timeout": timeout,
                "target": target,
                "addressee": addressee,
                **kw,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._answer


# ─── Channel.ask on Telegram ────────────────────────────────────────


class TestTelegramAsk:
    @pytest.mark.asyncio
    async def test_ask_with_options_sends_an_inline_keyboard_and_returns_the_chosen_option(
        self, telegram_sender
    ):
        from robothor.engine.channels import telegram_ask as tg_channel

        channel = TelegramChannel()
        task = asyncio.create_task(
            channel.ask(
                "Which vendor?",
                ["Acme", "Globex"],
                timeout=5.0,
                target="chat-1",
                addressee="op",
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        ask_ids = tg_channel.pending_ask_ids()
        assert len(ask_ids) == 1
        ask_id = ask_ids[0]

        telegram_sender.bot.send_message.assert_awaited_once()
        keyboard = telegram_sender.bot.send_message.await_args.kwargs["reply_markup"]
        callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        # The INDEX, not the option text: Telegram caps callback_data at 64
        # bytes and an option longer than that would be silently truncated
        # into a different answer.
        assert callback_data == [f"ask:{ask_id}:0", f"ask:{ask_id}:1"]

        assert (
            tg_channel.resolve_ask_choice(
                ask_id, 1, chat_id="chat-1", sender_id="op", owner_ok=True
            )
            == "Globex"
        )
        assert await task == "Globex"

    @pytest.mark.asyncio
    async def test_ask_without_options_resolves_from_a_plain_text_reply(self, telegram_sender):
        from robothor.engine.channels import telegram_ask as tg_channel

        channel = TelegramChannel()
        task = asyncio.create_task(
            channel.ask("When?", timeout=5.0, target="chat-1", addressee="op")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # No keyboard for a free-text ask — the question went out as text.
        telegram_sender.bot.send_message.assert_not_awaited()
        assert telegram_sender.sent and "When?" in telegram_sender.sent[0][1]

        assert tg_channel.resolve_ask_text("chat-1", "op", "Tuesday", owner_ok=True) is True
        assert await task == "Tuesday"

    @pytest.mark.asyncio
    async def test_a_reply_in_another_chat_does_not_answer_this_ask(self, telegram_sender):
        from robothor.engine.channels import telegram_ask as tg_channel

        channel = TelegramChannel()
        task = asyncio.create_task(
            channel.ask("When?", timeout=0.2, target="chat-1", addressee="op")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert tg_channel.resolve_ask_text("chat-2", "op", "Tuesday", owner_ok=True) is False
        assert await task is None

    @pytest.mark.asyncio
    async def test_ask_times_out_returning_none_rather_than_a_default_option(self, telegram_sender):
        from robothor.engine.channels import telegram_ask as tg_channel

        channel = TelegramChannel()
        answer = await channel.ask("Which?", ["Acme", "Globex"], timeout=0.05, target="chat-1")
        assert answer is None
        # And nothing is left behind to answer a question nobody is waiting on.
        assert tg_channel.pending_ask_ids() == []

    @pytest.mark.asyncio
    async def test_ask_with_no_target_answers_none_instead_of_guessing_a_chat(
        self, telegram_sender
    ):
        assert await TelegramChannel().ask("Which?", ["a"], timeout=1.0, target="") is None

    @pytest.mark.asyncio
    async def test_ask_returns_none_when_no_telegram_sender_is_registered(self):
        from robothor.engine import delivery

        previous = delivery._platform_senders.pop("telegram", None)
        try:
            assert await TelegramChannel().ask("Which?", timeout=1.0, target="chat-1") is None
        finally:
            if previous is not None:
                delivery._platform_senders["telegram"] = previous

    @pytest.mark.asyncio
    async def test_resolving_an_unknown_ask_id_is_a_no_op(self, telegram_sender):
        from robothor.engine.channels import telegram_ask as tg_channel

        assert (
            tg_channel.resolve_ask_choice(
                "no-such-ask", 0, chat_id="chat-1", sender_id="op", owner_ok=True
            )
            is None
        )
        assert tg_channel.resolve_ask_text("chat-1", "op", "Tuesday", owner_ok=True) is False


# ─── The run-status sink ────────────────────────────────────────────


class TestRunStatusSink:
    @pytest.mark.asyncio
    async def test_a_registered_sink_receives_the_event(self):
        seen: list[dict] = []

        async def sink(event):
            seen.append(event)

        run_status.register_status_sink("run-1", sink)
        assert await run_status.emit_status("run-1", {"event": "approval_required"}) is True
        assert seen == [{"event": "approval_required"}]

    @pytest.mark.asyncio
    async def test_emitting_for_a_run_with_no_sink_is_false_not_an_error(self):
        assert await run_status.emit_status("run-nobody", {"event": "approval_required"}) is False

    @pytest.mark.asyncio
    async def test_a_raising_sink_never_reaches_the_caller(self):
        async def sink(event):
            raise RuntimeError("SSE queue is gone")

        run_status.register_status_sink("run-1", sink)
        assert await run_status.emit_status("run-1", {"event": "x"}) is False

    def test_registering_none_registers_nothing(self):
        run_status.register_status_sink("run-1", None)
        assert run_status.status_sink_count() == 0

    @pytest.mark.asyncio
    async def test_unregistering_stops_delivery(self):
        seen: list[dict] = []

        async def sink(event):
            seen.append(event)

        run_status.register_status_sink("run-1", sink)
        run_status.unregister_status_sink("run-1")
        assert await run_status.emit_status("run-1", {"event": "x"}) is False
        assert seen == []

    def test_registering_a_live_session_arms_its_sink(self):
        """The one wiring point. ``AgentRunner.execute`` registers the session
        for the life of its loop and unregisters it in the same ``finally``; the
        sink rides that lifetime rather than a second one that must not
        diverge."""
        from robothor.engine import session_registry

        session = MagicMock()
        session.run_id = "run-wired"
        seen: list[dict] = []

        async def sink(event):
            seen.append(event)

        session_registry.register(session, on_status=sink)
        try:
            assert run_status.status_sink_count() == 1
        finally:
            session_registry.unregister(session)
        assert run_status.status_sink_count() == 0

    def test_the_runner_arms_it_with_the_runs_on_status(self):
        """Read from source: a full ``execute`` needs a model, and the claim
        here is that the call site exists at all — the defect this platform
        keeps finding is a correct function with an inert caller."""
        from pathlib import Path

        src = Path(run_status.__file__.replace("run_status.py", "runner.py")).read_text(
            encoding="utf-8"
        )
        assert "async with observe_request(session, on_status) as report_status:" in src
        runtime = Path(
            run_status.__file__.replace("run_status.py", "request_runtime.py")
        ).read_text()
        assert "session_registry.register(session, on_status=reporter.status)" in runtime
        assert "session_registry.unregister(session)" in runtime


# ─── The ask_user tool ──────────────────────────────────────────────


def _ctx(run_id: str = "run-1", identity=None) -> ToolContext:
    return ToolContext(
        agent_id="assistant", run_id=run_id, tenant_id="test-tenant", identity=identity
    )


class _Store:
    """An in-memory stand-in for the agent_questions DAL."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}
        self.answers: list[tuple[str, str]] = []
        # Recorded so a test can assert the tool does NOT expire its own row.
        # Nothing in the handler is wired to this; the watchdog owns expiry.
        self.expired: list[str] = []

    def ask_question(self, **kw):
        kw.setdefault("expires_at", datetime.now(UTC) + timedelta(seconds=300))
        row = SimpleNamespace(id=f"q-{len(self.rows) + 1}", **kw)
        self.rows[row.id] = row
        return row

    def answer_question(self, question_id, answer, *, answered_by, tenant_id=""):
        self.answers.append((question_id, answer))
        return True

    def expire_overdue_questions(self, *, tenant_id=""):
        self.expired.extend(self.rows)
        return [self.rows[rid] for rid in self.rows]


@pytest.fixture
def store(monkeypatch):
    from robothor.engine.tools.handlers import ask_user as module

    s = _Store()
    monkeypatch.setattr(module.agent_questions, "ask_question", s.ask_question)
    monkeypatch.setattr(module.agent_questions, "answer_question", s.answer_question)
    monkeypatch.setattr(
        module.agent_questions, "expire_overdue_questions", s.expire_overdue_questions
    )
    return s


def _run_row(trigger_type: str = "telegram", trigger_detail: str = "chat:chat-1|sender:Op"):
    return {"id": "run-1", "trigger_type": trigger_type, "trigger_detail": trigger_detail}


@pytest.fixture
def telegram_run(monkeypatch):
    from robothor.engine.tools.handlers import ask_user as module

    monkeypatch.setattr(module.tracking, "get_run", lambda run_id: _run_row())


def _webchat_identity(user_id: str = WEBCHAT_USER):
    return SimpleNamespace(
        channel="webchat",
        identifier=user_id,
        verified=True,
        role="member",
        user_account_id=user_id,
    )


@pytest.fixture
def webchat_run(monkeypatch):
    """A Helm run: the trigger detail carries the derived session key and the
    ctx carries the resolved identity, which is where the target comes from."""
    from robothor.engine.tools.handlers import ask_user as module

    monkeypatch.setattr(
        module.tracking,
        "get_run",
        lambda run_id: _run_row("webchat", f"webchat:agent:main:user:{WEBCHAT_USER}"),
    )


class TestAskUserTool:
    @pytest.mark.asyncio
    async def test_an_answer_from_the_channel_settles_the_row_and_is_returned(
        self, store, telegram_run
    ):
        channel = _RecordingChannel(answer="Globex")
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()(
                {"question": "Which vendor?", "options": ["Acme", "Globex"]}, _ctx()
            )

        assert result["answered"] is True
        assert result["answer"] == "Globex"
        assert store.answers == [("q-1", "Globex")]
        # The row is written BEFORE the channel is asked — a process that dies
        # mid-ask must still leave the question behind.
        assert store.rows["q-1"].channel == "telegram"
        assert store.rows["q-1"].target == "chat-1"
        assert channel.calls[0]["target"] == "chat-1"

    @pytest.mark.asyncio
    async def test_no_answer_leaves_the_row_unsettled_here_and_names_it(self, store, telegram_run):
        """The tool does NOT stamp the row itself.

        It stopped waiting; the operator has not stopped caring. The row is left
        as the DAL wrote it and the watchdog's sweep
        (``daemon._sweep_stale_questions``) is what marks it ``expired`` once
        the deadline passes — and ``agent_questions.answer_question`` accepts a
        late answer even then. A tool that settled the row on its own timeout
        would be throwing away the only useful thing left about the question.
        """
        channel = _RecordingChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert result["answered"] is False
        assert result["question_id"] == "q-1"
        assert store.answers == []
        assert store.expired == []
        assert "q-1" in result["message"]

    @pytest.mark.asyncio
    async def test_a_question_nobody_was_shown_says_so_rather_than_claiming_silence(
        self, store, webchat_run
    ):
        """``NoListenerError`` is not "the person did not reply".

        The webchat channel raises it when nothing is reading the run's status
        stream, so the question reached no screen at all. Reporting that as
        "asked and stayed silent" would be the fabricated ``delivered`` this
        whole module is written against — and it is what made the missing
        plan-execution branch in the Helm silent for a full tool budget.
        """
        from robothor.engine.channels.base import NoListenerError

        channel = _RowIdChannel(raises=NoListenerError("nobody is reading this run's stream"))
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert result["answered"] is False
        assert result["delivered"] is False
        assert result["reason"] == "no_listener"
        assert "nobody was connected to receive the question" in result["message"]
        # The row is named so a later turn can still pick the answer up.
        assert result["question_id"] == "q-1"

    @pytest.mark.asyncio
    async def test_a_channel_that_cannot_ask_says_nobody_could_be_asked(self, store, telegram_run):
        """``NotImplementedError`` is a legitimate outcome, not a crash — and it
        happens in the same tick, so the answer must not claim a wait."""
        channel = _RecordingChannel(raises=NotImplementedError("a bus has nobody to ask"))
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert result["answered"] is False
        assert result["delivered"] is False
        assert result["question_id"] == "q-1"
        assert "no channel could deliver" in result["message"]
        assert "300" not in result["message"]

    @pytest.mark.asyncio
    async def test_no_registered_channel_says_the_same(self, store, telegram_run):
        with patch("robothor.engine.channels.get_channel", return_value=None):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert result["answered"] is False
        assert result["delivered"] is False
        assert "no channel could deliver" in result["message"]

    @pytest.mark.asyncio
    async def test_a_delivered_question_nobody_answered_reports_the_measured_wait(
        self, store, telegram_run
    ):
        """The distinction the model needs: was the person not asked, or asked
        and silent? Only the second is a wait at all — and the number quoted is
        the one that elapsed, not the one that was requested. This fake returns
        instantly, so a message quoting 42 here would be the exact fabrication
        the `delivered` split exists to remove."""
        channel = _RecordingChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which?", "timeout_seconds": 42}, _ctx())

        assert result["answered"] is False
        assert result["delivered"] is True
        assert result["message"].startswith("No answer after 0s")
        assert "42" not in result["message"]
        assert "q-1" in result["message"]

    @pytest.mark.asyncio
    async def test_a_webchat_run_now_resolves_the_webchat_channel(
        self, store, webchat_run, monkeypatch
    ):
        """C9 ships a webchat channel, so a Helm run DOES wait: the browser
        answers the row through the bridge and the channel learns by polling it.

        Before C9 ``_CHANNEL_FOR_TRIGGER`` had no ``webchat`` entry, so the
        answer came back ``delivered: false`` in the same tick and only a later
        turn could see it.
        """
        channel = _RowIdChannel(answer="Globex")
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()(
                {"question": "Which vendor?", "options": ["Acme", "Globex"]}, _ctx()
            )

        assert result["answered"] is True
        assert result["answer"] == "Globex"
        assert store.answers == [("q-1", "Globex")]
        assert channel.calls[0]["question_id"] == "q-1"

    @pytest.mark.asyncio
    async def test_the_question_row_records_channel_webchat_and_the_user_target(
        self, store, monkeypatch
    ):
        """``target`` is the member's ``user_accounts.id`` — the address the
        channel writes the turn and the notification to. The resolved identity
        is the first place it is read from, ahead of the session key."""
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking,
            "get_run",
            lambda run_id: _run_row("webchat", "webchat:agent:main:primary"),
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx(identity=_webchat_identity()))

        assert store.rows["q-1"].channel == "webchat"
        assert store.rows["q-1"].target == WEBCHAT_USER
        assert channel.calls[0]["target"] == WEBCHAT_USER
        assert channel.calls[0]["addressee"] == WEBCHAT_USER

    @pytest.mark.parametrize(
        "detail_prefix",
        [
            # Every webchat entry point, from `chat.py`'s five endpoints. The
            # first cut of this fallback matched only `webchat:`, so it was dead
            # on the three plan shapes — including `plan-exec:`, the full-tools
            # execution run where `ask_user` actually fires.
            "webchat",
            "plan",
            "plan-exec",
            "plan-revise",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_webchat_target_falls_back_to_the_trigger_detail(
        self, store, monkeypatch, detail_prefix
    ):
        """An unverified or absent identity still has the session key, and the
        key carries the user segment the derivation put there."""
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking,
            "get_run",
            lambda run_id: _run_row("webchat", f"{detail_prefix}:agent:main:user:{WEBCHAT_USER}"),
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx())

        assert store.rows["q-1"].target == WEBCHAT_USER

    @pytest.mark.asyncio
    async def test_an_unknown_detail_prefix_yields_no_target(self, store, monkeypatch):
        """The PREFIX half of the guard, pinned on its own.

        ``_webchat_target`` checks two things — a prefix a webchat run actually
        writes, and the ``:user:`` segment — and a test that only ever feeds it
        well-formed keys proves one of them. Here the key is perfectly
        webchat-shaped and the prefix is not one of the four, so the answer must
        be "no target": a detail this code does not recognise is not a licence to
        read an id out of the middle of it.
        """
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking,
            "get_run",
            lambda run_id: _run_row("webchat", f"canvas:agent:main:user:{WEBCHAT_USER}"),
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx())

        assert store.rows["q-1"].target == ""

    @pytest.mark.asyncio
    async def test_a_webchat_prefix_without_a_user_segment_yields_no_target(
        self, store, monkeypatch
    ):
        """And the SEGMENT half, with a prefix that IS in the set. Between this
        and the test above, dropping either check reddens exactly one of them."""
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking,
            "get_run",
            lambda run_id: _run_row("webchat", "plan-exec:agent:main:primary"),
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx())

        assert store.rows["q-1"].target == ""

    @pytest.mark.asyncio
    async def test_a_telegram_shaped_detail_is_not_read_as_a_webchat_target(
        self, store, monkeypatch
    ):
        """``plan:`` is also a Telegram prefix, where the value is a chat id. The
        `:user:` segment is what makes it a webchat session key; without it there
        is no target rather than a chat id aimed at the wrong surface."""
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking, "get_run", lambda run_id: _run_row("webchat", "plan:chat-1|sender:Op")
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx())

        assert store.rows["q-1"].target == ""

    @pytest.mark.asyncio
    async def test_an_unverified_identity_is_not_used_as_the_target(self, store, monkeypatch):
        """Same direction as ``_addressee``: an unproven identity binds nothing.

        Without the ``verified`` check an unverified identity's ``user_account_id``
        would silently become the address a delivery is written to — and the
        channel writes into whatever session that id resolves to.
        """
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking, "get_run", lambda run_id: _run_row("webchat", "webchat:no-user-here")
        )
        unverified = SimpleNamespace(
            channel="webchat",
            identifier=WEBCHAT_USER,
            verified=False,
            role="member",
            user_account_id=WEBCHAT_USER,
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx(identity=unverified))

        assert store.rows["q-1"].target == ""
        assert channel.calls[0]["addressee"] == ""

    @pytest.mark.asyncio
    async def test_the_owner_shared_session_leaves_the_target_empty(self, store, monkeypatch):
        """``agent:main:primary`` names no user, and inventing one would aim a
        delivery at whoever happened to sort first."""
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking,
            "get_run",
            lambda run_id: _run_row("webchat", "webchat:agent:main:primary"),
        )
        channel = _RowIdChannel(answer=None)
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?"}, _ctx())

        assert store.rows["q-1"].target == ""

    @pytest.mark.asyncio
    async def test_telegram_ask_is_called_with_exactly_the_c10_arguments(self, store, telegram_run):
        """The row id reaches webchat through a capability flag, so Telegram's
        ``ask`` — whose signature takes no ``**kw`` — is never handed a kwarg it
        cannot accept. An unconditional extra would raise ``TypeError``, be
        swallowed as "the channel could not ask", and silently break every
        Telegram ask."""
        channel = _RecordingChannel(answer="Acme")
        assert getattr(channel, "ask_wants_question_id", False) is False

        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert result["answered"] is True
        assert set(channel.calls[0]) == {"question", "options", "timeout", "target", "addressee"}

    @pytest.mark.asyncio
    async def test_a_database_blip_on_the_settle_write_still_returns_the_answer(
        self, store, telegram_run, monkeypatch
    ):
        """The person answered. Losing that because the settle write failed
        would throw away the only thing the whole tool exists to obtain."""
        from robothor.engine.tools.handlers import ask_user as module

        def _boom(*a, **kw):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(module.agent_questions, "answer_question", _boom)
        channel = _RecordingChannel(answer="Acme")
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert result["answered"] is True
        assert result["answer"] == "Acme"

    @pytest.mark.asyncio
    async def test_a_database_blip_on_the_ask_write_is_an_error_not_a_crash(
        self, store, telegram_run, monkeypatch
    ):
        """No row means no durable question, so there is nothing honest to
        return but a refusal — and a bare traceback is not one."""
        from robothor.engine.tools.handlers import ask_user as module

        def _boom(*a, **kw):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(module.agent_questions, "ask_question", _boom)
        channel = _RecordingChannel(answer="Acme")
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert "error" in result
        assert channel.calls == []

    @pytest.mark.asyncio
    async def test_a_cron_run_is_refused_with_a_sentence_and_writes_no_row(
        self, store, monkeypatch
    ):
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(module.tracking, "get_run", lambda run_id: _run_row("cron", "nightly"))
        result = await _handler()({"question": "Which vendor?"}, _ctx())

        assert "error" in result
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_a_sub_agent_run_is_refused_too(self, store, monkeypatch):
        from robothor.engine.tools.handlers import ask_user as module

        monkeypatch.setattr(
            module.tracking, "get_run", lambda run_id: _run_row("sub_agent", "parent:run-0")
        )
        result = await _handler()({"question": "Which vendor?"}, _ctx())
        assert "error" in result
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_an_empty_question_is_refused(self, store, telegram_run):
        result = await _handler()({"question": "   "}, _ctx())
        assert "error" in result
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_it_emits_approval_required_with_the_row_id(self, store, telegram_run):
        seen: list[dict] = []

        async def sink(event):
            seen.append(event)

        run_status.register_status_sink("run-1", sink)
        channel = _RecordingChannel(answer="Acme")
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which vendor?", "options": ["Acme"]}, _ctx())

        assert len(seen) == 1
        assert seen[0]["event"] == "approval_required"
        assert seen[0]["kind"] == "question"
        assert seen[0]["id"] == "q-1"
        assert seen[0]["run_id"] == "run-1"
        assert seen[0]["question"] == "Which vendor?"
        assert seen[0]["options"] == ["Acme"]


class TestAskUserTimeoutArithmetic:
    """``registry.execute`` kills a tool at its timeout. An ask that waits
    longer than its own budget is a guaranteed ``TimeoutError`` rather than a
    question."""

    def test_the_tool_gets_a_budget_longer_than_the_default(self):
        from robothor.engine.runner import _resolve_tool_timeout

        assert _resolve_tool_timeout(ASK_USER, 120) >= 600

    def test_the_wait_is_capped_below_that_budget(self):
        from robothor.engine.runner import _resolve_tool_timeout
        from robothor.engine.tools.handlers.ask_user import bounded_timeout

        budget = _resolve_tool_timeout(ASK_USER, 120)
        # Strictly below, with headroom: equal would race the registry's own
        # asyncio.timeout and turn "nobody answered" into a tool failure.
        assert bounded_timeout(10_000) < budget
        assert bounded_timeout(30) == 30.0
        # Nonsense inputs land somewhere usable rather than at zero.
        assert bounded_timeout(0) > 0
        assert bounded_timeout(None) > 0
        assert bounded_timeout("not a number") > 0

    @pytest.mark.asyncio
    async def test_the_capped_timeout_is_what_reaches_the_channel(self, store, telegram_run):
        from robothor.engine.tools.handlers.ask_user import bounded_timeout

        channel = _RecordingChannel(answer="Acme")
        with patch("robothor.engine.channels.get_channel", return_value=channel):
            await _handler()({"question": "Which?", "timeout_seconds": 100_000}, _ctx())

        assert channel.calls[0]["timeout"] == bounded_timeout(100_000)


class TestAskUserIsRegistered:
    def test_the_handler_is_reachable_through_dispatch(self):
        from robothor.engine.tools.dispatch import _get_handlers

        assert ASK_USER in _get_handlers()

    def test_the_schema_is_published(self):
        from robothor.engine.tools.schemas import get_engine_schemas

        schema = get_engine_schemas()[ASK_USER]
        params = schema["function"]["parameters"]
        assert params["required"] == ["question"]
        assert "options" in params["properties"]
        assert params["properties"]["timeout_seconds"]["maximum"] == 600
