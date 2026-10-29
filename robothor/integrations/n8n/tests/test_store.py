"""MemoryN8nStore: the snapshot semantics PgN8nStore keeps on PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robothor.integrations.n8n.store import MemoryN8nStore

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
T = "test-tenant"


def _wf(wid: str, name: str, *, active: bool = True) -> dict:
    return {
        "workflow_id": wid,
        "name": name,
        "active": active,
        "tags": [],
        "remote_updated_at": None,
    }


def _ex(eid: str, wid: str, hours_ago: float) -> dict:
    at = NOW - timedelta(hours=hours_ago)
    return {
        "execution_id": eid,
        "workflow_id": wid,
        "status": "error",
        "mode": "trigger",
        "started_at": at,
        "stopped_at": at,
    }


async def test_record_returns_only_new_ids_and_is_per_tenant() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    assert await store.record_executions(T, [_ex("1", "wf1", 1), _ex("2", "wf1", 2)]) == ["1", "2"]
    assert await store.record_executions(T, [_ex("2", "wf1", 2), _ex("3", "wf1", 3)]) == ["3"]
    assert await store.record_executions("other-tenant", [_ex("1", "wf1", 1)]) == ["1"]


async def test_pending_alerts_until_marked() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.record_executions(T, [_ex("1", "wf1", 1), _ex("2", "wf1", 2)])
    assert {p["execution_id"] for p in await store.pending_alerts(T)} == {"1", "2"}
    await store.mark_alerted(T, ["1"])
    assert [p["execution_id"] for p in await store.pending_alerts(T)] == ["2"]
    assert await store.pending_alerts("other-tenant") == []


async def test_error_messages_are_kept_once_set() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.record_executions(T, [_ex("1", "wf1", 1)])
    await store.set_error_messages(T, {"1": "Bad gateway"})
    rows = await store.list_executions(T, "wf1")
    assert rows[0]["error_message"] == "Bad gateway"


async def test_workflow_failure_counts_and_last_error() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.upsert_workflows(T, [_wf("wf1", "Billing"), _wf("wf2", "Alerts", active=False)])
    await store.record_executions(
        T, [_ex("1", "wf1", 1), _ex("2", "wf1", 30), _ex("3", "wf1", 24 * 9)]
    )
    await store.set_error_messages(T, {"1": "newest", "2": "older"})

    rows = {r["workflow_id"]: r for r in await store.list_workflows(T, now=NOW)}
    assert rows["wf1"]["failures_24h"] == 1
    assert rows["wf1"]["failures_7d"] == 2
    assert rows["wf1"]["last_failure_at"] == NOW - timedelta(hours=1)
    assert rows["wf1"]["last_error"] == "newest"
    assert rows["wf2"]["failures_24h"] == 0
    assert rows["wf2"]["last_failure_at"] is None
    assert rows["wf2"]["active"] is False


async def test_a_workflow_gone_from_n8n_leaves_the_snapshot() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.upsert_workflows(T, [_wf("wf1", "Billing"), _wf("wf2", "Alerts")])
    await store.upsert_workflows(T, [_wf("wf1", "Billing v2")])
    rows = await store.list_workflows(T, now=NOW)
    assert [(r["workflow_id"], r["name"]) for r in rows] == [("wf1", "Billing v2")]


async def test_list_executions_newest_first_with_window_and_limit() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.record_executions(
        T, [_ex("1", "wf1", 5), _ex("2", "wf2", 1), _ex("3", "wf1", 2), _ex("4", "wf1", 48)]
    )
    assert [r["execution_id"] for r in await store.list_executions(T, "wf1")] == ["3", "1", "4"]
    assert [r["execution_id"] for r in await store.list_executions(T, "wf1", limit=1)] == ["3"]
    recent = await store.list_executions(T, None, since=NOW - timedelta(hours=24))
    assert [r["execution_id"] for r in recent] == ["2", "3", "1"]


async def test_sync_state_keeps_the_last_good_poll_through_a_failure() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    assert await store.get_sync_state(T) is None
    await store.set_sync_state(T, polled_at=NOW, ok=True, workflows_total=4, workflows_active=3)
    later = NOW + timedelta(minutes=2)
    await store.set_sync_state(T, polled_at=later, ok=False, error="auth (HTTP 401)")
    state = await store.get_sync_state(T)
    assert state is not None
    assert state.last_poll_at == later
    assert state.last_ok_at == NOW
    assert state.last_error == "auth (HTTP 401)"
    assert (state.workflows_total, state.workflows_active) == (4, 3)
    await store.set_sync_state(T, polled_at=later, ok=True, workflows_total=4, workflows_active=3)
    state = await store.get_sync_state(T)
    assert state is not None and state.last_error is None and state.last_ok_at == later


async def test_purge_drops_old_executions_only_for_the_tenant() -> None:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.record_executions(T, [_ex("1", "wf1", 1), _ex("2", "wf1", 24 * 31)])
    await store.record_executions("other-tenant", [_ex("9", "wf1", 24 * 31)])
    assert await store.purge(T, older_than_days=30) == 1
    assert [r["execution_id"] for r in await store.list_executions(T, None)] == ["1"]
    assert len(await store.list_executions("other-tenant", None)) == 1
