"""A Telegram message sent while the chat's run is working joins that run.

It used to wait in the coalescing buffer until the run finished, then went out
as its own turn: two answers, the first written without the new context.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.models import AgentRun, PlanState, RunStatus, TriggerType
from robothor.engine.telegram import TelegramBot

CHAT = "12345"


@pytest.fixture
def bot(engine_config):
    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
        patch("robothor.engine.telegram.save_exchange_async", new_callable=AsyncMock) as save,
        patch("robothor.engine.delivery._persist_delivery_status", new_callable=AsyncMock),
    ):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.delete_message = AsyncMock()
        mock_bot.send_chat_action = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock()
        mock_bot_cls.return_value = mock_bot
        save.return_value = None

        tg = TelegramBot(engine_config, MagicMock())
        tg.bot = mock_bot
        tg.send_message = AsyncMock(return_value=[MagicMock(message_id=99)])
        tg.save_spy = save
        yield tg
    _sessions.clear()


def _run(text: str) -> AgentRun:
    return AgentRun(
        id="run-live-1",
        status=RunStatus.COMPLETED,
        output_text=text,
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail=f"chat:{CHAT}",
    )


def _session(bot):
    key = bot._session_key(CHAT)
    return key, get_shared_session(key)


async def _send(bot, text: str, message_id: str = "501") -> None:
    """What handle_text does after its intercepts: stash the id, enqueue."""
    key, session = _session(bot)
    bot._user_message_id_buffers[CHAT] = message_id
    await bot._enqueue_message(CHAT, key, session, text)


async def _settle(bot) -> None:
    for _ in range(50):
        task = bot._active_tasks.get(CHAT)
        if task is None and not bot._drain_scheduled.get(CHAT):
            return
        if task is not None:
            await task
        else:
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_message_mid_run_joins_the_run(bot):
    started, release = asyncio.Event(), asyncio.Event()
    inboxes = []

    async def execute(**kwargs):
        inboxes.append(kwargs["live_inbox"])
        started.set()
        await release.wait()
        kwargs["live_inbox"].take()  # what the loop does at its next safe point
        return _run("Draft, including Y.")

    bot.runner.execute = AsyncMock(side_effect=execute)
    await _send(bot, "draft the summary", "500")
    await started.wait()

    await _send(bot, "also include Y", "501")
    release.set()
    await _settle(bot)

    assert bot.runner.execute.await_count == 1, "no second run"
    assert [i.text for i in inboxes[0].taken] == ["also include Y"]
    bot.bot.set_message_reaction.assert_awaited_once()
    assert bot.bot.set_message_reaction.await_args.kwargs["message_id"] == 501
    _, session = _session(bot)
    user_rows = [m["content"] for m in session.history if m["role"] == "user"]
    assert len(user_rows) == 1
    assert "draft the summary" in user_rows[0] and "also include Y" in user_rows[0]
    await asyncio.sleep(0)
    stored_user_text = bot.save_spy.await_args.args[1]
    assert "also include Y" in stored_user_text


@pytest.mark.asyncio
async def test_a_failed_run_still_records_what_was_added(bot):
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await release.wait()
        kwargs["live_inbox"].take()
        raise RuntimeError("provider down")

    bot.runner.execute = AsyncMock(side_effect=execute)
    await _send(bot, "draft the summary", "500")
    await started.wait()
    await _send(bot, "also include Y", "501")
    release.set()
    await _settle(bot)

    _, session = _session(bot)
    user_rows = [m["content"] for m in session.history if m["role"] == "user"]
    assert len(user_rows) == 1 and "also include Y" in user_rows[0]
    assert bot.runner.execute.await_count == 1, "taken, so not re-run as a new turn"


@pytest.mark.asyncio
async def test_a_message_the_run_never_took_becomes_a_follow_up_turn(bot):
    started, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def execute(**kwargs):
        messages.append(kwargs["message"])
        if len(messages) == 1:
            started.set()
            await release.wait()  # finishes WITHOUT reaching another safe point
        return _run(f"answer {len(messages)}")

    bot.runner.execute = AsyncMock(side_effect=execute)
    await _send(bot, "first", "500")
    await started.wait()
    await _send(bot, "second", "501")
    release.set()
    await _settle(bot)

    assert messages[0] == "first"
    assert messages[1:] == ["second"], "not lost, not merged into the first"


@pytest.mark.asyncio
async def test_stop_discards_what_was_sent_mid_run(bot):
    started = asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await asyncio.sleep(30)

    bot.runner.execute = AsyncMock(side_effect=execute)
    await _send(bot, "long job", "500")
    await started.wait()
    await _send(bot, "never mind that", "501")

    stop = MagicMock()
    stop.chat.id = int(CHAT)
    stop.answer = AsyncMock()
    await bot.cmd_stop(stop)
    await asyncio.sleep(0.5)

    assert bot.runner.execute.await_count == 1
    assert not bot._message_buffers.get(CHAT)


@pytest.mark.asyncio
async def test_a_second_sender_in_a_group_does_not_join_anothers_run(bot):
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        if not started.is_set():
            started.set()
            await release.wait()
        return _run("ok")

    bot.runner.execute = AsyncMock(side_effect=execute)
    key, session = _session(bot)
    alice = {"telegram_user_id": "1001", "display_name": "Alice"}
    bob = {"telegram_user_id": "1002", "display_name": "Bob"}
    await bot._enqueue_message(CHAT, key, session, "alice asks", sender_info=alice)
    await started.wait()
    await bot._enqueue_message(CHAT, key, session, "bob asks", sender_info=bob)
    release.set()
    await _settle(bot)

    assert bot.runner.execute.await_count == 2
    bot.bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_plan_execution_drains_what_queued_behind_it(bot):
    """Its `finally` popped the active task and never drained: a message sent
    during plan execution sat in the buffer until the NEXT message arrived."""
    key, session = _session(bot)
    session.active_plan = PlanState(
        plan_id="plan-1",
        plan_text="Do the thing",
        original_message="do the thing",
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
        creator_sender_info={"display_name": "Alice", "telegram_user_id": "1001"},
    )
    bot._build_background_config = MagicMock(return_value=MagicMock())
    calls = []

    async def execute(**kwargs):
        calls.append(kwargs.get("message"))
        if len(calls) == 1:
            bot._message_buffers.setdefault(CHAT, []).append("queued during plan")
        return _run("done")

    bot.runner.execute = AsyncMock(side_effect=execute)
    with patch("robothor.engine.telegram_plan_mode.save_plan_state_async", new=AsyncMock()):
        task = asyncio.create_task(bot._execute_approved_plan(CHAT, key, session))
        bot._active_tasks[CHAT] = task
        await task
    await _settle(bot)

    assert calls[-1] == "queued during plan"
