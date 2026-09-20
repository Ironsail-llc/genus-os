"""Persistent contexts have bounded lifetimes and retain read-only evidence on uncertain effects."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.tests.test_workflow_store import prepared
from robothor.autonomy.workflows.manager import WorkflowManager


@pytest.fixture
async def opened(store, identity, monkeypatch):
    from robothor.autonomy import inspection as module

    clock = [0.0]
    page = SimpleNamespace(goto=AsyncMock(), url="https://form.example/apply")
    context = SimpleNamespace(
        route=AsyncMock(), set_offline=AsyncMock(), new_page=AsyncMock(return_value=page)
    )
    page.context = context
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(module, "inspect_page", AsyncMock(return_value={"fields": []}))
    manager = WorkflowManager(store, AsyncMock(return_value=browser), clock=lambda: clock[0])
    operation = prepared(store, identity)
    result = await manager.open(identity, "main", operation["id"], page.url)
    yield manager, result, operation, browser, clock
    await manager.shutdown()


async def test_expiry_releases_the_browser_without_inventing_uncertainty(opened, store, identity):
    """Rewritten 2026-09-20: this asserted `reconciling` for an operation that
    had never submitted. Losing a browser is not evidence of an external
    effect; calling it uncertain pinned the reservation against a result that
    never existed, and it reached that state through a raw UPDATE the journal's
    transition table forbids."""
    manager, result, operation, browser, clock = opened
    clock[0] = 901
    await manager.expire_idle()
    assert manager.active_count == 0
    browser.close.assert_awaited_once()
    assert store.operation(identity, operation["id"])["state"] == "failed"
    assert (await manager.status(identity, "main", result["workflow_id"]))["state"] == "lost"


async def test_inspection_cannot_extend_absolute_lifetime(opened, identity):
    manager, result, _, browser, clock = opened
    for moment in (800, 1600, 2400, 3200):
        clock[0] = moment
        await manager.inspect(identity, "main", result["workflow_id"])
    clock[0] = 3601
    with pytest.raises(PermissionError, match="workflow_lost"):
        await manager.inspect(identity, "main", result["workflow_id"])
    browser.close.assert_awaited_once()


async def test_uncertain_action_freezes_context_and_returns_cached_result(opened, identity):
    manager, result, _, browser, _ = opened
    wid = result["workflow_id"]
    live = manager._live[wid]
    live.broker.execute_on_page = AsyncMock(
        return_value={"state": "reconciling", "reason": "unknown_outcome"}
    )
    command = str(uuid4())
    plan = ExecutionPlan(url="https://form.example/apply", submit_selector="#submit")
    first = await manager.execute(identity, "main", wid, command, 0, plan)
    assert manager.active_count == 1
    browser.close.assert_not_awaited()
    live.page.context.set_offline.assert_awaited_once_with(True)
    assert await manager.execute(identity, "main", wid, command, 0, plan) == first
    live.broker.execute_on_page.assert_awaited_once()


async def test_close_is_owner_bound_and_returns_a_never_submitted_reservation(
    opened, store, identity
):
    """Rewritten 2026-09-20: the old name and assertion said closing a
    workflow must not release the budget. That is right once something has
    been committed -- and is covered for a submitted operation by
    test_kill_switch.py -- but here nothing was ever submitted, so holding the
    reservation burned the monthly cap against nothing."""
    manager, result, operation, browser, _ = opened
    wid = result["workflow_id"]
    with pytest.raises(PermissionError):
        await manager.close(identity.model_copy(update={"owner_id": "bob"}), "main", wid)
    browser.close.assert_not_awaited()
    await manager.close(identity, "main", wid)
    assert store.operation(identity, operation["id"])["state"] == "failed"
    assert store.spending_projection(identity)["months"].get("USD", {}) == {}
    browser.close.assert_awaited_once()


async def test_status_waits_for_in_progress_browser_open(store, identity, monkeypatch):
    from robothor.autonomy import inspection as module

    entered, release = asyncio.Event(), asyncio.Event()
    page = SimpleNamespace(goto=AsyncMock(), url="https://form.example/apply")
    context = SimpleNamespace(
        route=AsyncMock(), set_offline=AsyncMock(), new_page=AsyncMock(return_value=page)
    )
    page.context = context
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(module, "inspect_page", AsyncMock(return_value={"fields": []}))

    async def factory():
        entered.set()
        await release.wait()
        return browser

    manager = WorkflowManager(store, factory)
    op = prepared(store, identity)
    opening = asyncio.create_task(manager.open(identity, "main", op["id"], page.url))
    await entered.wait()
    wid = store.operation(identity, op["id"])["workflow_id"]
    checking = asyncio.create_task(manager.status(identity, "main", wid))
    try:
        await asyncio.sleep(0.05)
        assert not checking.done(), "opening page must not be declared lost"
        release.set()
        await opening
        assert (await checking)["state"] == "open"
        assert store.operation(identity, op["id"])["state"] == "reserved"
    finally:
        release.set()
        await opening
        await checking
        await manager.shutdown()


async def test_drain_refuses_new_work_but_preserves_existing_page(opened, store, identity):
    manager, result, operation, browser, _ = opened
    manager.drain()
    assert not manager.accepting
    refused = await manager.open(identity, "main", operation["id"], "https://form.example/apply")
    assert refused == {"error": "workflow_broker_draining"}
    inspected = await manager.inspect(identity, "main", result["workflow_id"])
    assert inspected["workflow_id"] == result["workflow_id"]
    assert manager.active_count == 1
    browser.close.assert_not_awaited()
    manager.resume_admission()
    assert manager.accepting
