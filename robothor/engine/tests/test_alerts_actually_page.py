"""Controls that report on themselves must not lie about what they did.

Two found by a sweep for built-but-unreachable controls:

* The alert self-test fired at ``info`` while its docstring claimed to
  verify the ``alert() -> send_fn(chat_id, text)`` path end-to-end — the one
  thing ``info`` cannot do, since ``_PAGE_LEVELS`` is
  ``frozenset({"critical"})``. Raising it to ``critical`` made it honest and
  made it a pager: the engine restarts, so it paged the operator CRITICAL on
  every start (52 pages in 7 days, none an incident). It is now a DIGEST
  probe that says so — it must write its ``alert_digest`` row, and it must
  not reach the Telegram sender. Real delivery is proved by the paths that
  page for real, not by an alert the engine fires at itself on every boot.

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
async def test_the_selftest_writes_its_row_without_paging(monkeypatch):
    """The probe must reach durable storage and stop there.

    Both halves matter. A probe that writes nothing proves nothing; a probe
    that pages fires CRITICAL on every engine start, which is how a pager
    gets muted.
    """
    from robothor.engine.daemon import _maybe_run_alert_selftest

    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")
    rows: list[tuple[str, str, str]] = []

    async def fake_write(notification_type, level, title, body, metadata):
        rows.append((notification_type, level, title))
        return True

    with (
        patch.object(alerts, "_send_telegram", AsyncMock()) as sender,
        patch.object(alerts, "_write_notification", AsyncMock(side_effect=fake_write)),
    ):
        await _maybe_run_alert_selftest()

    assert rows, "the self-test wrote no notification row — it proves nothing"
    assert rows[0][0] == "alert_digest", rows
    assert rows[0][1] not in alerts._PAGE_LEVELS, rows
    assert sender.await_count == 0, "the self-test paged the operator"


@pytest.mark.asyncio
async def test_the_selftest_is_silent_when_not_requested(monkeypatch):
    from robothor.engine.daemon import _maybe_run_alert_selftest

    monkeypatch.delenv("ROBOTHOR_ALERT_SELFTEST", raising=False)
    with (
        patch.object(alerts, "_send_telegram", AsyncMock()) as sender,
        patch.object(alerts, "_write_notification", AsyncMock(return_value=True)) as writer,
    ):
        await _maybe_run_alert_selftest()
    assert sender.await_count == 0
    assert writer.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_selftest_is_loud(monkeypatch, caplog):
    """A probe that fails quietly is worse than no probe: it reads as success.
    The level dropped to info; the loudness did not."""
    import logging

    from robothor.engine.daemon import _maybe_run_alert_selftest

    monkeypatch.setenv("ROBOTHOR_ALERT_SELFTEST", "1")
    caplog.set_level(logging.ERROR)
    with patch.object(alerts, "_write_notification", AsyncMock(return_value=False)):
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
