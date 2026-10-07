"""The Microsoft Graph transport: headers, paging, retries, error mapping."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from robothor.workspace.errors import (
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    RateLimited,
    UnknownEffect,
    WorkspaceError,
)
from robothor.workspace.microsoft.graph import GraphClient, merge_prefer
from robothor.workspace.tests.fake_graph import FakeGraphTenant

MAILBOX = "assistant@example.com"
MESSAGES = f"/users/{MAILBOX}/messages"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant, value: str = "static-token") -> None:
        tenant.issued_tokens.append(value)
        self.value = value
        self.calls = 0

    async def token(self) -> str:
        self.calls += 1
        return self.value


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def tenant() -> FakeGraphTenant:
    return FakeGraphTenant()


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def client(tenant, sleeps):
    graph = GraphClient(StaticToken(tenant), transport=tenant.transport(), sleep=sleeps)
    yield graph
    await graph.aclose()


def _seed(tenant: FakeGraphTenant, count: int) -> None:
    box = tenant.mailbox(MAILBOX)
    box.messages.extend({"id": f"m{i}", "subject": f"s{i}"} for i in range(count))


# ── headers ───────────────────────────────────────────────────────────────


async def test_default_headers_are_sent(client, tenant) -> None:
    _seed(tenant, 1)
    await client.get(MESSAGES)
    request = tenant.requests[-1]
    assert request.headers["authorization"] == "Bearer static-token"
    assert request.headers["prefer"] == 'IdType="ImmutableId", outlook.timezone="UTC"'
    uuid.UUID(request.headers["client-request-id"])


async def test_every_request_gets_its_own_client_request_id(client, tenant) -> None:
    _seed(tenant, 1)
    await client.get(MESSAGES)
    await client.get(MESSAGES)
    ids = {r.headers["client-request-id"] for r in tenant.requests}
    assert len(ids) == 2


async def test_caller_prefer_is_combined_not_replaced(client, tenant) -> None:
    _seed(tenant, 1)
    await client.get(MESSAGES, headers={"Prefer": 'outlook.body-content-type="text"'})
    prefer = tenant.requests[-1].headers["prefer"]
    assert prefer == (
        'IdType="ImmutableId", outlook.timezone="UTC", outlook.body-content-type="text"'
    )
    assert len(tenant.requests[-1].headers.get_list("prefer")) == 1


def test_merge_prefer_lets_the_caller_override_one_preference() -> None:
    merged = merge_prefer(
        'IdType="ImmutableId", outlook.timezone="UTC"', 'outlook.timezone="Europe/Berlin"'
    )
    assert merged == 'IdType="ImmutableId", outlook.timezone="Europe/Berlin"'


def test_merge_prefer_respects_quoted_commas() -> None:
    merged = merge_prefer('a="x, y"', 'b="z"')
    assert merged == 'a="x, y", b="z"'


# ── paging ────────────────────────────────────────────────────────────────


async def test_get_all_follows_next_links(client, tenant) -> None:
    _seed(tenant, 25)
    items = await client.get_all(MESSAGES, params={"$top": 10})
    assert [m["id"] for m in items] == [f"m{i}" for i in range(25)]
    assert len(tenant.requests) == 3


async def test_get_all_stops_at_max_items(client, tenant) -> None:
    _seed(tenant, 25)
    items = await client.get_all(MESSAGES, params={"$top": 10}, max_items=12)
    assert len(items) == 12
    assert len(tenant.requests) == 2


async def test_foreign_next_link_is_refused(client, tenant) -> None:
    _seed(tenant, 25)
    tenant.next_link_origin = "https://graph.microsoft.com.evil.example"
    with pytest.raises(WorkspaceError, match="nextLink"):
        await client.get_all(MESSAGES, params={"$top": 10})
    # The bearer token never went to the foreign origin.
    assert all(r.url.host == "graph.microsoft.com" for r in tenant.requests)


async def test_http_next_link_on_the_same_host_is_refused(client, tenant) -> None:
    _seed(tenant, 25)
    tenant.next_link_origin = "http://graph.microsoft.com"
    with pytest.raises(WorkspaceError, match="nextLink"):
        await client.get_all(MESSAGES, params={"$top": 10})


# ── path validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["users/x/messages", "https://evil.example/users", "//evil.example/x", "/users/../admin"],
)
async def test_paths_must_be_relative_to_the_base(client, bad) -> None:
    with pytest.raises(WorkspaceError):
        await client.get(bad)


# ── read retries ──────────────────────────────────────────────────────────


async def test_read_retries_429_honouring_retry_after(client, tenant, sleeps) -> None:
    _seed(tenant, 1)
    tenant.fail("GET", r"/users/[^/]+/messages", 429, headers={"Retry-After": "7"})
    result = await client.get(MESSAGES)
    assert result["value"][0]["id"] == "m0"
    assert sleeps.calls == [7.0]
    assert len(tenant.requests) == 2


@pytest.mark.parametrize("status", [503, 504])
async def test_read_retries_transient_5xx(client, tenant, sleeps, status) -> None:
    _seed(tenant, 1)
    tenant.fail("GET", r"/users/[^/]+/messages", status)
    await client.get(MESSAGES)
    assert len(tenant.requests) == 2
    assert len(sleeps.calls) == 1


async def test_read_gives_up_after_three_attempts(client, tenant, sleeps) -> None:
    tenant.fail("GET", r"/users/[^/]+/messages", 429, headers={"Retry-After": "2"}, times=5)
    with pytest.raises(RateLimited) as info:
        await client.get(MESSAGES)
    assert len(tenant.requests) == 3
    assert sleeps.calls == [2.0, 2.0]
    assert info.value.retry_after == 2.0


async def test_read_does_not_wait_past_the_total_budget(client, tenant, sleeps) -> None:
    tenant.fail("GET", r"/users/[^/]+/messages", 429, headers={"Retry-After": "120"})
    with pytest.raises(RateLimited) as info:
        await client.get(MESSAGES)
    assert sleeps.calls == []
    assert len(tenant.requests) == 1
    assert info.value.retry_after == 120.0


async def test_read_budget_is_cumulative(client, tenant, sleeps) -> None:
    tenant.fail("GET", r"/users/[^/]+/messages", 429, headers={"Retry-After": "40"}, times=3)
    with pytest.raises(RateLimited):
        await client.get(MESSAGES)
    assert sleeps.calls == [40.0]  # a second 40 s wait would exceed 60 s in total
    assert len(tenant.requests) == 2


async def test_get_all_retries_a_throttled_page(client, tenant, sleeps) -> None:
    _seed(tenant, 15)
    await client.get(MESSAGES)  # warm-up so the fault hits page two
    tenant.requests.clear()
    tenant.fail("GET", r"/users/[^/]+/messages", 429, headers={"Retry-After": "1"})
    items = await client.get_all(MESSAGES, params={"$top": 10})
    assert len(items) == 15


# ── writes are never retried ──────────────────────────────────────────────


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
async def test_write_5xx_is_unknown_effect_and_not_retried(client, tenant, sleeps, method) -> None:
    _seed(tenant, 1)
    tenant.fail(method.upper(), r"/users/[^/]+/messages(/[^/]+)?", 503, times=5)
    path = MESSAGES if method == "post" else f"{MESSAGES}/m0"
    kwargs = {} if method == "delete" else {"json": {"subject": "x"}}
    with pytest.raises(UnknownEffect):
        await getattr(client, method)(path, **kwargs)
    assert len(tenant.requests) == 1
    assert sleeps.calls == []


async def test_write_transport_error_after_send_is_unknown_effect(client, tenant) -> None:
    tenant.fail("POST", r"/users/[^/]+/messages", raise_exc=httpx.ReadTimeout, times=5)
    with pytest.raises(UnknownEffect):
        await client.post(MESSAGES, json={"subject": "x"})
    assert len(tenant.requests) == 1


async def test_write_connect_error_is_not_unknown_effect(client, tenant) -> None:
    """A connection that never opened sent nothing: that is a plain failure."""
    tenant.fail("POST", r"/users/[^/]+/messages", raise_exc=httpx.ConnectError, times=5)
    with pytest.raises(WorkspaceError) as info:
        await client.post(MESSAGES, json={"subject": "x"})
    assert not isinstance(info.value, UnknownEffect)
    assert len(tenant.requests) == 1


async def test_write_429_is_rate_limited_without_retry(client, tenant, sleeps) -> None:
    tenant.fail("POST", r"/users/[^/]+/messages", 429, headers={"Retry-After": "3"})
    with pytest.raises(RateLimited) as info:
        await client.post(MESSAGES, json={"subject": "x"})
    assert info.value.retry_after == 3.0
    assert len(tenant.requests) == 1
    assert sleeps.calls == []


async def test_successful_writes_round_trip(client, tenant) -> None:
    created = await client.post(MESSAGES, json={"subject": "hello"})
    assert created["id"] == "msg-1"
    updated = await client.patch(f"{MESSAGES}/msg-1", json={"subject": "bye"})
    assert updated["subject"] == "bye"
    assert await client.delete(f"{MESSAGES}/msg-1") == {}
    assert tenant.mailbox(MAILBOX).messages == []


# ── error mapping ─────────────────────────────────────────────────────────


async def test_404_is_not_found(client, tenant) -> None:
    with pytest.raises(NotFound) as info:
        await client.get(f"{MESSAGES}/nope")
    assert info.value.status == 404
    assert info.value.code == "ErrorItemNotFound"


async def test_412_is_precondition_failed(client, tenant) -> None:
    tenant.mailbox(MAILBOX).messages.append({"id": "m0", "@odata.etag": 'W/"1"'})
    with pytest.raises(PreconditionFailed):
        await client.patch(f"{MESSAGES}/m0", json={"subject": "x"}, headers={"If-Match": 'W/"0"'})


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_permission_denied(client, tenant, status) -> None:
    tenant.fail("GET", r"/users/[^/]+/messages", status)
    with pytest.raises(PermissionDenied):
        await client.get(MESSAGES)
    assert len(tenant.requests) == 1


async def test_write_4xx_is_a_definite_failure_not_unknown_effect(client, tenant) -> None:
    tenant.fail("POST", r"/users/[^/]+/messages", 400)
    with pytest.raises(WorkspaceError) as info:
        await client.post(MESSAGES, json={})
    assert not isinstance(info.value, UnknownEffect)
    assert info.value.status == 400


async def test_errors_carry_graph_code_and_message_but_not_the_body(client, tenant) -> None:
    tenant.fail(
        "GET",
        r"/users/[^/]+/messages",
        400,
        body={
            "error": {"code": "ErrorInvalidRequest", "message": "Bad filter."},
            "debug": "customer-private-content",
        },
    )
    with pytest.raises(WorkspaceError) as info:
        await client.get(MESSAGES)
    text = str(info.value)
    assert "ErrorInvalidRequest" in text
    assert "Bad filter." in text
    assert "customer-private-content" not in text
    assert "static-token" not in text


async def test_errors_never_carry_the_token(tenant, sleeps) -> None:
    secret_token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.leak.leak"

    class Leaky:
        async def token(self) -> str:
            return secret_token

    tenant.fail(
        "GET",
        r"/users/[^/]+/messages",
        401,
        body={"error": {"code": "InvalidAuthenticationToken", "message": secret_token}},
    )
    graph = GraphClient(Leaky(), transport=tenant.transport(), sleep=sleeps)
    with pytest.raises(PermissionDenied) as info:
        await graph.get(MESSAGES)
    await graph.aclose()
    assert secret_token not in str(info.value)


# ── per-mailbox concurrency ───────────────────────────────────────────────


async def test_at_most_four_requests_in_flight_per_mailbox(client, tenant) -> None:
    tenant.latency = 0.02
    _seed(tenant, 1)
    other = "owner@example.com"
    tenant.mailbox(other).messages.append({"id": "o1"})

    await asyncio.gather(
        *(client.get(MESSAGES) for _ in range(12)),
        *(client.get(f"/users/{other}/messages") for _ in range(12)),
    )

    assert tenant.max_inflight[MAILBOX] == 4
    assert tenant.max_inflight[other] == 4


async def test_mailbox_key_is_case_and_encoding_insensitive(client, tenant) -> None:
    tenant.latency = 0.02
    _seed(tenant, 1)
    spellings = [MESSAGES, MESSAGES.replace("assistant", "ASSISTANT"), MESSAGES.replace("@", "%40")]
    await asyncio.gather(*(client.get(spellings[i % 3]) for i in range(12)))
    assert tenant.max_inflight[MAILBOX] == 4
