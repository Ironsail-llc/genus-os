"""Provider contracts use an isolated transport, never live customer accounts."""

import json

import httpx
import pytest

from robothor.sales.providers import (
    Instantly,
    Pipedrive,
    ProviderError,
    RateLimited,
    UnknownEffect,
)


@pytest.mark.asyncio
async def test_pipedrive_uses_tenant_vault_and_header_not_url():
    reads = []

    def secret(key, *, tenant_id):
        reads.append((key, tenant_id))
        return "example-account" if key.endswith("company_domain") else "test-secret"

    def respond(request):
        assert request.url.host == "example-account.pipedrive.com"
        assert request.url.path == "/api/v1/users/me"
        assert request.headers["x-api-token"] == "test-secret"
        assert "test-secret" not in str(request.url)
        return httpx.Response(200, json={"success": True, "data": {"id": 7}})

    p = Pipedrive("tenant-a", secret_get=secret, transport=httpx.MockTransport(respond))
    assert await p.identity() == {"id": 7}
    assert all(t == "tenant-a" for _, t in reads)


@pytest.mark.asyncio
async def test_missing_or_malicious_provider_configuration_never_requests():
    def secret(key, **kwargs):
        return "evil.example/a?x=" if key.endswith("company_domain") else "test-secret"

    with pytest.raises(ProviderError):
        await Pipedrive("tenant-a", secret_get=secret).identity()
    with pytest.raises(ProviderError):
        await Instantly("tenant-a", secret_get=lambda *a, **kw: None).accounts()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,error", [("GET", ProviderError), ("POST", UnknownEffect)])
async def test_timeout_never_retries_or_leaks_secrets(method, error):
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("test-secret with private request body")

    p = Instantly(
        "tenant-a",
        secret_get=lambda *a, **kw: "test-secret",
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(error) as caught:
        await p.request(method, "/api/v2/emails/reply", payload={"body": "private"})
    assert len(calls) == 1
    assert "test-secret" not in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_rate_limit_and_redirect_are_not_followed():
    p = Instantly(
        "tenant-a",
        secret_get=lambda *a, **kw: "test-secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "75"})),
    )
    with pytest.raises(RateLimited) as caught:
        await p.accounts()
    assert caught.value.retry_after == 75
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    p.transport = httpx.MockTransport(redirect)
    with pytest.raises(ProviderError):
        await p.accounts()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_exact_reply_contract_and_one_step_initial_campaign():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "provider-receipt"})

    p = Instantly(
        "tenant-a",
        secret_get=lambda *a, **kw: "test-secret",
        transport=httpx.MockTransport(respond),
    )
    draft = {
        "sender": "sales@example.com",
        "recipient": "alice@example.com",
        "subject": "A question",
        "body": "Hello <Alice>\nCan we help?",
        "reply_to_uuid": "thread-1",
    }
    await p.reply(draft)
    body = json.loads(requests[-1].content)
    assert body == {
        "eaccount": draft["sender"],
        "reply_to_uuid": "thread-1",
        "subject": draft["subject"],
        "body": {"text": draft["body"], "html": "Hello &lt;Alice&gt;<br/>Can we help?"},
    }
    await p.create_campaign("action-1", draft, "America/Chicago", "2026-09-18")
    campaign = json.loads(requests[-1].content)
    assert len(campaign["sequences"]) == 1
    assert len(campaign["sequences"][0]["steps"]) == 1
    assert campaign["email_list"] == [draft["sender"]]
    assert campaign["stop_on_reply"] is True
    assert campaign["open_tracking"] is False
    assert campaign["campaign_schedule"]["end_date"] == "2026-09-18"
    assert campaign["daily_limit"] == 1
    assert "status" not in campaign  # activation is a separate guarded operation


@pytest.mark.asyncio
async def test_verification_uses_separate_purchase_and_poll_contracts():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"email": "alice@example.com", "verification_status": "pending"}
        )

    provider = Instantly(
        "tenant-a",
        secret_get=lambda *a, **kw: "test-secret",
        transport=httpx.MockTransport(respond),
    )
    await provider.start_verification("alice@example.com")
    await provider.verification("alice@example.com")
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v2/email-verification"
    assert json.loads(requests[0].content) == {"email": "alice@example.com"}
    assert requests[1].method == "GET"
    assert requests[1].url.path == "/api/v2/email-verification/alice@example.com"


@pytest.mark.asyncio
async def test_email_scans_pin_workspace_filter_cursor_and_shared_rate_limit(ops):
    requests = []

    def secret(key, **kwargs):
        return "workspace-1" if key.endswith("workspace_id") else "test-secret"

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/v2/workspaces/current":
            return httpx.Response(200, json={"id": "workspace-1"})
        assert request.url.path == "/api/v2/emails"
        assert dict(request.url.params) == {
            "limit": "100",
            "campaign_id": "campaign-1",
            "starting_after": "page-2",
            "min_timestamp_created": "2026-09-01T00:00:00Z",
            "max_timestamp_created": "2026-09-18T00:00:00Z",
            "sort_order": "asc",
            "latest_of_thread": "false",
        }
        return httpx.Response(200, json={"items": []})

    api = Instantly(ops.tenant, secret_get=secret, transport=httpx.MockTransport(respond))
    for _ in range(20):
        await api.emails(
            campaign_id="campaign-1",
            cursor="page-2",
            min_timestamp_created="2026-09-01T00:00:00Z",
            max_timestamp_created="2026-09-18T00:00:00Z",
            sort_order="asc",
            latest_of_thread=False,
        )
    with pytest.raises(RateLimited):
        await Instantly(
            ops.tenant, secret_get=secret, transport=httpx.MockTransport(respond)
        ).emails()
    assert len([r for r in requests if r.url.path == "/api/v2/emails"]) == 20


@pytest.mark.asyncio
async def test_email_scan_refuses_wrong_workspace_even_if_page_would_be_empty(ops):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "another-workspace"})

    api = Instantly(
        ops.tenant, secret_get=lambda *a, **k: "workspace-1", transport=httpx.MockTransport(respond)
    )
    with pytest.raises(ProviderError):
        await api.emails()
    assert [r.url.path for r in calls] == ["/api/v2/workspaces/current"]


@pytest.mark.asyncio
async def test_email_scan_rejects_vault_rotation_away_from_pinned_workspace(ops):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "new-workspace"})

    api = Instantly(
        ops.tenant,
        secret_get=lambda *a, **kw: "new-workspace",
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(ProviderError):
        await api.emails(workspace_id="original-workspace")
    assert calls == []


@pytest.mark.asyncio
async def test_pipedrive_identity_inspection_uses_canonical_read_endpoints():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        return httpx.Response(200, json={"success": True, "data": {"id": 11}})

    provider = Pipedrive(
        "tenant-a",
        secret_get=lambda key, **kw: (
            "example-account" if key.endswith("company_domain") else "test-secret"
        ),
        transport=httpx.MockTransport(respond),
    )
    await provider.organization(11)
    await provider.person_record(22)
    await provider.lead("00000000-0000-4000-8000-000000000001")
    assert paths == [
        "/api/v2/organizations/11",
        "/api/v2/persons/22",
        "/api/v1/leads/00000000-0000-4000-8000-000000000001",
    ]
