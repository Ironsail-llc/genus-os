"""The bridge contract: row builders and the status the n8n section shows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.integrations.n8n.contract import (
    execution_row,
    executions_payload,
    status_for,
    workflow_row,
    workflows_payload,
)
from robothor.integrations.n8n.store import MemoryN8nStore, N8nSyncState

BASE = "https://n8n.example.com"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _state(*, ok_ago: float | None = 30, error: str | None = None) -> N8nSyncState:
    return N8nSyncState(
        tenant_id="test-tenant",
        last_poll_at=NOW - timedelta(seconds=10),
        last_ok_at=None if ok_ago is None else NOW - timedelta(seconds=ok_ago),
        last_error=error,
        workflows_total=3,
        workflows_active=2,
    )


QUIET = [{"failures_24h": 0}]
FAILING = [{"failures_24h": 0}, {"failures_24h": 2}]


@pytest.mark.parametrize(
    ("configured", "state", "rows", "expected"),
    [
        (False, _state(), QUIET, "not_configured"),
        (False, None, FAILING, "not_configured"),
        (True, None, QUIET, "unknown"),
        (True, _state(ok_ago=None, error="auth (HTTP 401)"), QUIET, "unknown"),
        (True, _state(ok_ago=361), QUIET, "unknown"),
        (True, _state(ok_ago=359), QUIET, "ok"),
        (True, _state(), FAILING, "warn"),
        (True, _state(error="network"), QUIET, "warn"),
        (True, _state(), [], "ok"),
    ],
)
def test_status_matrix(configured, state, rows, expected) -> None:
    assert status_for(state, 120, rows, configured=configured, now=NOW) == expected


def test_status_accepts_a_mapping_for_the_sync_state() -> None:
    state = {"last_ok_at": NOW - timedelta(seconds=5), "last_error": None}
    assert status_for(state, 120, QUIET, now=NOW) == "ok"


def test_workflow_row_has_exactly_the_contract_keys() -> None:
    row = workflow_row(
        {
            "workflow_id": "wf1",
            "name": "Invoice sync",
            "active": True,
            "tags": ["billing"],
            "remote_updated_at": NOW,
            "failures_24h": 1,
            "failures_7d": 4,
            "last_failure_at": NOW - timedelta(hours=1),
            "last_error": "Request failed with status 500",
        },
        BASE + "/",
    )
    assert row == {
        "workflow_id": "wf1",
        "name": "Invoice sync",
        "active": True,
        "tags": ["billing"],
        "failures_24h": 1,
        "failures_7d": 4,
        "last_failure_at": "2026-10-29T11:00:00+00:00",
        "last_error": "Request failed with status 500",
        "last_failure_url": "",
        "url": "https://n8n.example.com/workflow/wf1",
    }


def test_workflow_row_links_the_newest_failed_execution() -> None:
    row = workflow_row({"workflow_id": "wf1", "last_failed_execution_id": "ex7"}, BASE)
    assert row["last_failure_url"] == "https://n8n.example.com/workflow/wf1/executions/ex7"


def test_workflow_row_has_no_failure_link_without_a_failure_or_a_base_url() -> None:
    assert workflow_row({"workflow_id": "wf1"}, BASE)["last_failure_url"] == ""
    no_base = workflow_row({"workflow_id": "wf1", "last_failed_execution_id": "ex7"}, "")
    assert no_base["last_failure_url"] == ""


def test_execution_row_has_exactly_the_contract_keys() -> None:
    row = execution_row(
        {
            "execution_id": "42",
            "workflow_id": "wf1",
            "status": "error",
            "mode": "trigger",
            "started_at": NOW,
            "stopped_at": None,
            "error_message": None,
        },
        BASE,
    )
    assert row == {
        "execution_id": "42",
        "status": "error",
        "mode": "trigger",
        "started_at": "2026-10-29T12:00:00+00:00",
        "stopped_at": None,
        "error_message": None,
        "url": "https://n8n.example.com/workflow/wf1/executions/42",
    }


async def _seeded() -> MemoryN8nStore:
    store = MemoryN8nStore(now=lambda: NOW)
    await store.upsert_workflows(
        "test-tenant",
        [
            {
                "workflow_id": "wf1",
                "name": "Invoice sync",
                "active": True,
                "tags": ["billing"],
                "remote_updated_at": None,
            }
        ],
    )
    await store.record_executions(
        "test-tenant",
        [
            {
                "execution_id": "42",
                "workflow_id": "wf1",
                "status": "error",
                "mode": "trigger",
                "started_at": NOW - timedelta(hours=2),
                "stopped_at": NOW - timedelta(hours=2),
            }
        ],
    )
    await store.set_sync_state(
        "test-tenant", polled_at=NOW, ok=True, workflows_total=1, workflows_active=1
    )
    return store


async def test_workflows_payload_is_the_bridge_response() -> None:
    store = await _seeded()
    payload = await workflows_payload(
        store, "test-tenant", base_url=BASE, poll_seconds=120, configured=True, now=NOW
    )
    assert payload["status"] == "warn"
    assert payload["base_url"] == BASE
    assert payload["last_poll_at"] == NOW.isoformat()
    assert payload["last_ok_at"] == NOW.isoformat()
    assert payload["last_error"] is None
    assert [w["workflow_id"] for w in payload["workflows"]] == ["wf1"]
    assert payload["workflows"][0]["failures_24h"] == 1
    assert set(payload) == {
        "status",
        "base_url",
        "last_poll_at",
        "last_ok_at",
        "last_error",
        "workflows",
    }


async def test_an_unconfigured_payload_reads_nothing() -> None:
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"an unconfigured payload must not read the store ({name})")

    payload = await workflows_payload(
        Untouchable(), "test-tenant", base_url="", poll_seconds=120, configured=False, now=NOW
    )
    assert payload == {
        "status": "not_configured",
        "base_url": "",
        "last_poll_at": None,
        "last_ok_at": None,
        "last_error": None,
        "workflows": [],
    }


async def test_executions_payload_is_the_bridge_response() -> None:
    store = await _seeded()
    payload = await executions_payload(store, "test-tenant", "wf1", base_url=BASE, limit=20)
    assert [e["execution_id"] for e in payload["executions"]] == ["42"]
    assert payload["executions"][0]["url"].endswith("/workflow/wf1/executions/42")
    assert set(payload) == {"executions"}
