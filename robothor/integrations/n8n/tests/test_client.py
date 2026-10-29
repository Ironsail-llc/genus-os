"""The n8n public-API client: pagination, error classes, and a key that never leaks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest

from robothor.integrations.n8n.client import MAX_EXECUTIONS, N8nClient, N8nError

BASE = "https://n8n.example.com"
KEY = "n8n-api-key-0123456789abcdef"


def _client(handler) -> N8nClient:
    return N8nClient(BASE, KEY, transport=httpx.MockTransport(handler))


def _wf(n: int, *, active: bool = True) -> dict:
    return {
        "id": f"wf{n}",
        "name": f"Workflow {n}",
        "active": active,
        "tags": [{"id": "t1", "name": "billing"}],
        "createdAt": "2026-10-01T10:00:00.000Z",
        "updatedAt": "2026-10-02T11:30:00.000Z",
        "nodes": [{"name": "Start", "parameters": {"secret": "do-not-keep"}}],
    }


def _ex(n: int, workflow: str = "wf1") -> dict:
    return {
        "id": str(n),
        "workflowId": workflow,
        "status": "error",
        "mode": "trigger",
        "startedAt": "2026-10-28T09:00:00.000Z",
        "stoppedAt": "2026-10-28T09:00:05.000Z",
        "finished": False,
    }


async def test_workflows_follow_the_cursor_and_keep_only_the_summary() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["X-N8N-API-KEY"] == KEY
        assert request.url.path == "/api/v1/workflows"
        if request.url.params.get("cursor") == "page2":
            return httpx.Response(200, json={"data": [_wf(3, active=False)], "nextCursor": None})
        return httpx.Response(200, json={"data": [_wf(1), _wf(2)], "nextCursor": "page2"})

    async with _client(handler) as client:
        rows = await client.list_workflows()

    assert [r["workflow_id"] for r in rows] == ["wf1", "wf2", "wf3"]
    assert rows[0] == {
        "workflow_id": "wf1",
        "name": "Workflow 1",
        "active": True,
        "tags": ["billing"],
        "remote_updated_at": datetime(2026, 10, 2, 11, 30, tzinfo=UTC),
    }
    assert rows[2]["active"] is False
    assert "nodes" not in rows[0]
    assert seen[0].url.params["limit"] == "250"
    assert "cursor" not in seen[0].url.params


async def test_error_executions_page_and_stop_at_the_cap() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/v1/executions"
        assert request.url.params["status"] == "error"
        assert request.url.params["includeData"] == "false"
        page = int(request.url.params.get("cursor") or 0)
        data = [_ex(page * 100 + i) for i in range(100)]
        return httpx.Response(200, json={"data": data, "nextCursor": str(page + 1)})

    async with _client(handler) as client:
        rows = await client.list_error_executions()

    assert len(rows) == MAX_EXECUTIONS == 500
    assert len(calls) == 5
    first = rows[0]
    assert first["execution_id"] == "0"
    assert first["workflow_id"] == "wf1"
    assert first["status"] == "error"
    assert first["mode"] == "trigger"
    assert first["started_at"] == datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert first["stopped_at"] == datetime(2026, 10, 28, 9, 0, 5, tzinfo=UTC)


async def test_error_executions_can_be_narrowed_to_one_workflow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["workflowId"] == "wf7"
        assert request.url.params["limit"] == "20"
        return httpx.Response(200, json={"data": [_ex(1, "wf7")], "nextCursor": None})

    async with _client(handler) as client:
        rows = await client.list_error_executions(limit=20, workflow_id="wf7")

    assert [r["workflow_id"] for r in rows] == ["wf7"]


async def test_healthz_answers_from_the_instance_root() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})

    async with _client(handler) as client:
        assert await client.healthz() is True


async def test_a_trailing_slash_or_api_suffix_on_the_base_url_is_tolerated() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [], "nextCursor": None})

    for base in (f"{BASE}/", f"{BASE}/api/v1", f"{BASE}/api/v1/"):
        async with N8nClient(base, KEY, transport=httpx.MockTransport(handler)) as client:
            await client.list_workflows()
    assert paths == ["/api/v1/workflows"] * 3


async def test_the_error_message_of_one_execution_is_read_from_its_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/executions/42"
        assert request.url.params["includeData"] == "true"
        body = {
            "id": "42",
            "data": {"resultData": {"error": {"message": "Request failed with status 500"}}},
        }
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        assert await client.get_execution_error("42") == "Request failed with status 500"


async def test_an_execution_without_an_error_reads_as_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "42", "data": {"resultData": {}}})

    async with _client(handler) as client:
        assert await client.get_execution_error("42") is None


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "auth"), (403, "auth"), (429, "rate_limit"), (502, "network"), (404, "unknown")],
)
async def test_http_failures_raise_a_classified_error(status: int, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": f"bad key {KEY}"})

    async with _client(handler) as client:
        with pytest.raises(N8nError) as caught:
            await client.list_workflows()

    assert caught.value.kind == kind
    assert caught.value.status == status
    assert KEY not in str(caught.value)
    assert KEY not in repr(caught.value)


async def test_a_network_failure_is_classified_and_carries_no_key(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused while sending {KEY}", request=request)

    caplog.set_level(logging.DEBUG)
    async with _client(handler) as client:
        with pytest.raises(N8nError) as caught:
            await client.healthz()

    assert caught.value.kind == "network"
    assert caught.value.status is None
    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert KEY not in caplog.text


async def test_the_key_never_appears_in_the_client_repr_or_logs(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [_wf(1)], "nextCursor": None})

    caplog.set_level(logging.DEBUG)
    client = _client(handler)
    async with client:
        await client.list_workflows()
    assert KEY not in repr(client)
    assert KEY not in str(client)
    assert KEY not in caplog.text
    assert BASE in repr(client)
