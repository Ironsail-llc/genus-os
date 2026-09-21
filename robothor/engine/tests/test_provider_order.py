"""Manifest routing stays model-specific, concurrent-safe and budget constrained."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.config import manifest_to_agent_config
from robothor.engine.llm_client import LLMClient
from robothor.engine.request_budget import RequestBudgetError, openrouter_quote
from robothor.engine.tests.test_request_budget import endpoint

MODEL = "openrouter/example/model"


def test_manifest_routing_is_validated_and_copied():
    routing = {MODEL: ["preferred/fp8", "backup"]}
    config = manifest_to_agent_config({"id": "example", "model": {"provider_order": routing}})
    assert config.provider_order == routing
    routing[MODEL].clear()
    assert config.provider_order[MODEL] == ["preferred/fp8", "backup"]
    assert manifest_to_agent_config({"id": "example"}).provider_order == {}


@pytest.mark.parametrize(
    "routing",
    [
        [],
        "provider",
        {MODEL: []},
        {MODEL: "provider"},
        {MODEL: [""]},
        {MODEL: [True]},
        {"local/model": ["provider"]},
    ],
)
def test_invalid_manifest_routing_is_rejected(routing):
    with pytest.raises(ValueError, match="provider_order"):
        manifest_to_agent_config({"id": "example", "model": {"provider_order": routing}})


@pytest.mark.parametrize("streaming", [False, True])
async def test_routing_is_scoped_to_session_and_exact_model(monkeypatch, streaming):
    client = LLMClient()
    seen = {}

    async def dispatch(messages, *args, **kwargs):
        await asyncio.sleep(0)
        seen[messages[0]["content"]] = [
            client._build_llm_kwargs(model, messages, [], 100, 0.3, stream=streaming)
            for model in (MODEL, "openrouter/example/fallback", "openrouter/anthropic/example")
        ]
        return object()

    monkeypatch.setattr(client, "_call_llm", dispatch)
    monkeypatch.setattr(client, "_call_llm_streaming", dispatch)

    async def run(name, routing):
        session = SimpleNamespace(
            provider_order=routing,
            messages=[{"role": "user", "content": name}],
            run=SimpleNamespace(id=name, trigger_type="sub_agent"),
        )
        await client._do_llm_call(
            session, [MODEL], [], (lambda _: None) if streaming else None, set(), 0.3
        )

    await asyncio.gather(run("pinned", {MODEL: ["preferred"]}), run("default", {}))
    provider = seen["pinned"][0].get("extra_body", {}).get("provider", {})
    assert provider == {"only": ["preferred"], "order": ["preferred"], "allow_fallbacks": False}
    assert "provider" not in seen["pinned"][1].get("extra_body", {})
    assert seen["pinned"][2]["extra_body"]["provider"]["order"] == ["Anthropic"]
    assert "provider" not in seen["default"][0].get("extra_body", {})
    assert "provider" not in client._build_llm_kwargs(MODEL, [], [], 100, 0.3).get("extra_body", {})


async def test_routing_restored_after_error(monkeypatch):
    client = LLMClient()
    monkeypatch.setattr(client, "_call_llm", AsyncMock(side_effect=RuntimeError("unavailable")))
    session = SimpleNamespace(
        provider_order={MODEL: ["preferred"]},
        messages=[],
        run=SimpleNamespace(id="run", trigger_type="sub_agent"),
    )
    with pytest.raises(RuntimeError):
        await client._do_llm_call(session, [MODEL], [], None, set(), 0.3)
    assert "provider" not in client._build_llm_kwargs(MODEL, [], [], 100, 0.3).get("extra_body", {})


def test_budget_respects_preference_then_price_without_broadening_eligibility():
    cheap = endpoint(tag="cheap")
    preferred = endpoint(
        tag="preferred/fp8", pricing={"prompt": "0.000002", "completion": "0.000003"}
    )
    request = {
        "max_tokens": 100,
        "extra_body": {"provider": {"order": ["preferred", "cheap"], "allow_fallbacks": False}},
    }
    units, bounded = openrouter_quote(request, [cheap, preferred])
    assert units == 2300
    assert bounded["extra_body"]["provider"]["only"] == ["preferred/fp8"]
    request["extra_body"]["provider"]["ignore"] = ["preferred"]
    assert openrouter_quote(request, [cheap, preferred])[1]["extra_body"]["provider"]["only"] == [
        "cheap"
    ]
    request["extra_body"]["provider"]["only"] = ["preferred"]
    with pytest.raises(RequestBudgetError):
        openrouter_quote(request, [cheap, preferred])


def test_manifest_cannot_override_required_anthropic_backend(monkeypatch):
    from robothor.engine.provider_routing import provider_order_scope

    with (
        provider_order_scope({"openrouter/anthropic/example": ["other"]}),
        pytest.raises(ValueError),
    ):
        LLMClient._build_llm_kwargs("openrouter/anthropic/example", [], [], 100, 0.3)


async def test_inherited_scope_closes_without_leaking_preferences():
    from robothor.engine.provider_routing import provider_order_scope

    ready = asyncio.Event()

    async def inherited():
        await ready.wait()
        return LLMClient._build_llm_kwargs(MODEL, [], [], 100, 0.3)

    with provider_order_scope({MODEL: ["preferred"]}):
        task = asyncio.create_task(inherited())
    ready.set()
    assert "provider" not in (await task).get("extra_body", {})
