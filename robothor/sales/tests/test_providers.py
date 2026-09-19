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
