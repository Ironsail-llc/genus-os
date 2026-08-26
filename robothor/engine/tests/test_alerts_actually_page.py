"""Controls that report on themselves must not lie about what they did.

Two found by a sweep for built-but-unreachable controls:

* The alert self-test fires at ``info``. ``_PAGE_LEVELS`` is
  ``frozenset({"critical"})``, so ``info`` writes a database row and never
  touches the Telegram sender. Its own docstring says it exists "so the
  alert() -> send_fn(chat_id, text) path can be verified end-to-end" — the
  one thing it cannot do. An operator sets the flag, sees no error, and
  concludes pages work; a revoked bot token or unset chat id is invisible.

* A blocked workflow's "immediate page" is not a page. It alerts at
  ``warning``, which is also a database row, and the code then writes a
  second row to the same table — the two channels documented as independent
  are one channel, and a workflow blocked on the operator never interrupts
  them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import alerts


@pytest.mark.asyncio
async def test_the_selftest_exercises_the_telegram_sender(monkeypatch):
    """Anything short of the real sender proves nothing about delivery."""
    from robothor.engine.daemon import _maybe_run_alert_selftest

    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")
    sent: list[tuple[str, str]] = []

    async def fake_send(level, title, body):
        sent.append((level, title))
        return True

    with patch.object(alerts, "_send_telegram", AsyncMock(side_effect=fake_send)):
        await _maybe_run_alert_selftest()

    assert sent, "the self-test never reached the Telegram sender"
    assert sent[0][0] in alerts._PAGE_LEVELS


@pytest.mark.asyncio
async def test_the_selftest_is_silent_when_not_requested(monkeypatch):
    from robothor.engine.daemon import _maybe_run_alert_selftest

    monkeypatch.delenv("ROBOTHOR_ALERT_SELFTEST", raising=False)
    with patch.object(alerts, "_send_telegram", AsyncMock()) as sender:
        await _maybe_run_alert_selftest()
    assert sender.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_selftest_is_loud(monkeypatch, caplog):
    """A probe that fails quietly is worse than no probe: it reads as success."""
    import logging

    from robothor.engine.daemon import _maybe_run_alert_selftest

    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")
    caplog.set_level(logging.ERROR)
    with patch.object(alerts, "_send_telegram", AsyncMock(return_value=False)):
        await _maybe_run_alert_selftest()

    assert "self-test" in caplog.text.lower()


@pytest.mark.asyncio
async def test_a_blocked_workflow_actually_pages():
    """The approval request blocks the run until a human answers it."""
    from robothor.engine import workflow as wf

    levels: list[str] = []

    async def fake_alert(level, title, body, **kw):
        levels.append(level)
        return True

    class _Req:
        prompt = "Approve deploy?"
        detail = "step 3 of 7"

    class _Run:
        id = "r-1"
        workflow_id = "wf-1"
        run_id = "r-1"

    spawned: list[object] = []

    class _Registry:
        def spawn(self, coro, name=None):
            spawned.append(coro)

    engine = wf.WorkflowEngine.__new__(wf.WorkflowEngine)

    class _Step:
        name = "deploy"

    with (
        patch("robothor.engine.alerts.alert", fake_alert),
        patch("robothor.engine.task_registry.get_task_registry", lambda: _Registry()),
        patch("robothor.crm.dal.send_notification", lambda **kw: None),
    ):
        engine._notify_approval_request(_Run(), _Step(), _Req())

    for coro in spawned:
        await coro

    assert levels, "no alert was raised at all"
    assert levels[0] in alerts._PAGE_LEVELS, (
        f"a blocked workflow alerted at {levels[0]!r}, which never leaves the database"
    )
