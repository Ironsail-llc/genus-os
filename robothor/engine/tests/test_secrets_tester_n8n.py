"""The ``n8n`` credential tester: aimed at the configured instance, by class only."""

from __future__ import annotations

import httpx
import pytest

from robothor.secrets import testers

BASE = "https://n8n.example.com"
KEY = "n8n-api-key-0123456789abcdef"


@pytest.fixture
def base_url(monkeypatch):
    from robothor.settings import reset_settings

    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("ROBOTHOR_N8N_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("ROBOTHOR_N8N_BASE_URL", value)
        reset_settings()

    yield _set
    reset_settings()


@pytest.mark.parametrize(
    "key", ["robothor_n8n_api_key", "integrations/n8n/api_key", "providers/n8n/api_key"]
)
def test_an_n8n_key_is_recognised(key) -> None:
    assert testers.kind_for_key(key) == "n8n"


async def test_the_probe_dials_the_configured_instance_with_the_header(base_url) -> None:
    base_url(BASE + "/")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "wf1"}], "nextCursor": "c"})

    outcome = await testers.probe("n8n", KEY, transport=httpx.MockTransport(handler))

    assert outcome.ok is True
    assert outcome.identity_hint == "1 workflow(s)"
    assert str(seen[0].url) == f"{BASE}/api/v1/workflows?limit=1"
    assert seen[0].headers["X-N8N-API-KEY"] == KEY


async def test_a_rejected_key_is_an_auth_failure(base_url) -> None:
    base_url(BASE)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    outcome = await testers.probe("n8n", KEY, transport=httpx.MockTransport(handler))
    assert outcome.ok is False and outcome.error_class == "auth"


async def test_without_a_base_url_the_probe_cannot_be_aimed(base_url) -> None:
    base_url(None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing to dial")

    outcome = await testers.probe("n8n", KEY, transport=httpx.MockTransport(handler))
    assert outcome.ok is False and outcome.error_class == "not_configured"


async def test_an_insecure_base_url_is_never_dialled(base_url) -> None:
    base_url("http://n8n.example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the key must not go out over plain http")

    outcome = await testers.probe("n8n", KEY, transport=httpx.MockTransport(handler))
    assert outcome.ok is False and outcome.error_class == "not_configured"


async def test_the_escape_hatch_lets_an_internal_http_instance_be_probed(
    base_url, monkeypatch
) -> None:
    monkeypatch.setenv("ROBOTHOR_N8N_ALLOW_INSECURE_HTTP", "true")
    base_url("http://n8n.internal.example.com:5678")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    outcome = await testers.probe("n8n", KEY, transport=httpx.MockTransport(handler))
    assert outcome.ok is True
    assert seen == ["http://n8n.internal.example.com:5678/api/v1/workflows?limit=1"]
