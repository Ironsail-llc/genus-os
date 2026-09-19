"""Cost admission covers each actual provider attempt, including auxiliary work."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.request_budget import (
    RequestBudget,
    RequestBudgetError,
    bounded_completion,
    budget_scope,
    openrouter_quote,
)


def response(cost):
    return SimpleNamespace(usage={"cost": cost})


async def quote(kwargs):
    return 60, {**kwargs, "num_retries": 0}


@pytest.mark.asyncio
async def test_concurrent_calls_share_admission_and_actual_cost_is_reusable():
    started, release = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        started.set()
        await release.wait()
        return response("0.000020")

    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        first = asyncio.create_task(bounded_completion(provider, model="example/model"))
        await started.wait()
        with pytest.raises(RequestBudgetError):
            await bounded_completion(provider, model="example/model")
        release.set()
        await first
        assert budget.charged_units == 20
        await bounded_completion(provider, model="example/model")
        assert budget.charged_units == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_unknown_request_keeps_allowance_and_retry_cannot_escape(failure):
    call = AsyncMock(side_effect=failure)
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        with pytest.raises(failure):
            await bounded_completion(call, model="example/model")
        with pytest.raises(RequestBudgetError):
            await bounded_completion(call, model="example/model")
    assert call.await_count == 1
    assert budget.charged_units == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [None, "NaN", "-1", True])
async def test_missing_or_invalid_cost_is_never_free(cost):
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        await bounded_completion(AsyncMock(return_value=response(cost)), model="example/model")
    assert budget.charged_units == 60


@pytest.mark.asyncio
async def test_provider_reported_sdk_header_settles_cost_but_local_estimates_do_not():
    reported = response(None)
    reported._hidden_params = {
        "additional_headers": {"llm_provider-x-litellm-response-cost": "0.000020"}
    }
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        await bounded_completion(AsyncMock(return_value=reported), model="example/model")
    assert budget.charged_units == 20

    estimated = response(None)
    estimated._hidden_params = {"response_cost": 0.000020}
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        await bounded_completion(AsyncMock(return_value=estimated), model="example/model")
    assert budget.charged_units == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", ["NaN", "-1", True])
async def test_invalid_provider_reported_sdk_cost_keeps_reservation(cost):
    reported = response(None)
    reported._hidden_params = {"additional_headers": {"llm_provider-x-litellm-response-cost": cost}}
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        await bounded_completion(AsyncMock(return_value=reported), model="example/model")
    assert budget.charged_units == 60


@pytest.mark.asyncio
async def test_stream_only_releases_allowance_on_clean_completion_and_closes_source():
    closed = []

    async def stream():
        try:
            yield response("0.000010")
            yield response("0.000020")
        finally:
            closed.append(True)

    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        result = await bounded_completion(
            AsyncMock(return_value=stream()), model="example/model", stream=True
        )
        await result.__anext__()
        assert budget.charged_units == 60
        await result.aclose()
        assert budget.charged_units == 60
    assert closed == [True]
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        result = await bounded_completion(
            AsyncMock(return_value=stream()), model="example/model", stream=True
        )
        async for _ in result:
            pass
        assert budget.charged_units == 20


@pytest.mark.asyncio
async def test_detached_child_cannot_spend_after_parent_scope_ends():
    release = asyncio.Event()
    call = AsyncMock(return_value=response(0))

    async def child():
        await release.wait()
        return await bounded_completion(call, model="example/model")

    with budget_scope(RequestBudget(100, quote=quote)):
        task = asyncio.create_task(child())
    release.set()
    with pytest.raises(RequestBudgetError):
        await task
    call.assert_not_called()


@pytest.mark.asyncio
async def test_main_and_auxiliary_paths_use_same_budget(monkeypatch):
    import litellm

    from robothor.engine.llm_client import _gated_acompletion
    from robothor.engine.pooled_completion import acompletion

    provider = AsyncMock(return_value=response("0.000060"))
    monkeypatch.setattr(litellm, "acompletion", provider)
    monkeypatch.setattr("robothor.engine.key_pool.api_key_for_model", lambda model: None)
    with budget_scope(RequestBudget(100, quote=quote)):
        await _gated_acompletion("openrouter/example/model", {"model": "openrouter/example/model"})
        with pytest.raises(RequestBudgetError):
            await acompletion(model="openrouter/example/model")
    assert provider.await_count == 1


def endpoint(**changes):
    return {
        "tag": "example/fp8",
        "context_length": 1000,
        "max_completion_tokens": 500,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "supported_parameters": ["max_tokens", "tools"],
        **changes,
    }


def test_quote_uses_full_context_and_provider_rate_limits_not_token_estimates():
    kwargs = {
        "model": "openrouter/example/model",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 100,
    }
    units, bounded = openrouter_quote(kwargs, [endpoint()])
    assert units == 1200  # full context input + capped output, integer micro-USD
    assert bounded["num_retries"] == 0
    assert bounded["api_base"] == "https://openrouter.ai/api/v1"
    provider = bounded["extra_body"]["provider"]
    assert provider["only"] == ["example/fp8"]
    assert provider["allow_fallbacks"] is False
    assert provider["require_parameters"] is True
    assert provider["max_price"] == {"prompt": 1, "completion": 2, "request": 0, "image": 0}
    assert "extra_body" not in kwargs


def test_json_request_skips_cheaper_endpoint_without_format_support():
    cheap = endpoint(tag="cheap/fp8")
    compatible = endpoint(
        tag="compatible/fp8",
        supported_parameters=["max_tokens", "response_format"],
        pricing={"prompt": "0.000002", "completion": "0.000003"},
    )
    kwargs = {"max_tokens": 100, "response_format": {"type": "json_object"}}
    _, bounded = openrouter_quote(kwargs, [cheap, compatible])
    assert bounded["extra_body"]["provider"]["only"] == ["compatible/fp8"]
    assert bounded["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_unsupported_json_mode_fails_before_reserving_or_dispatching():
    async def unsupported_quote(kwargs):
        return openrouter_quote(kwargs, [endpoint()])

    provider = AsyncMock(return_value=response(None))
    budget = RequestBudget(10_000, quote=unsupported_quote)
    with budget_scope(budget), pytest.raises(RequestBudgetError):
        await bounded_completion(
            provider, max_tokens=100, response_format={"type": "json_object"}
        )
    provider.assert_not_called()
    assert budget.charged_units == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"plugins": [{"id": "web"}]},
        {"n": 2},
        {"extra_body": {"models": ["expensive/model"]}},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
                    ],
                }
            ]
        },
    ],
)
def test_unpriced_features_fail_before_provider(extra):
    with pytest.raises(RequestBudgetError):
        openrouter_quote(
            {"model": "openrouter/example/model", "max_tokens": 100, **extra}, [endpoint()]
        )


def test_unbounded_prices_or_unsupported_existing_route_fail_closed():
    with pytest.raises(RequestBudgetError):
        openrouter_quote(
            {"model": "openrouter/example/model"},
            [endpoint(pricing={"prompt": "NaN", "completion": "0"})],
        )
    with pytest.raises(RequestBudgetError):
        openrouter_quote(
            {"model": "openrouter/example/model", "extra_body": {"provider": {"only": ["other"]}}},
            [endpoint()],
        )


@pytest.mark.asyncio
async def test_unpriced_search_provider_is_skipped_in_bounded_scope(monkeypatch):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import web

    paid = AsyncMock(return_value=[{"url": "https://example.com"}])
    local = AsyncMock(return_value={"results": [], "provider": "searxng"})
    monkeypatch.setattr(web, "_brave_search", paid)
    monkeypatch.setattr(web, "_scraped_search_with_fallback", local)
    with budget_scope(RequestBudget(100, quote=quote)):
        result = await web._search_with_fallback("example", 1, "auto", ToolContext())
        assert result["provider"] == "searxng"
        assert "budget" in result["brave_skipped"].lower()
        refused = await web._search_with_fallback("example", 1, "perplexity", ToolContext())
        assert "error" in refused
    paid.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_attempt_blocks_retry_and_fallback_in_real_client(monkeypatch):
    from robothor.engine import llm_client
    from robothor.engine.llm_client import LLMClient
    from robothor.engine.model_breaker import ModelBreaker

    provider = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(llm_client.litellm, "acompletion", provider)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: ModelBreaker(on_open=None))
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0)
    monkeypatch.setattr(LLMClient, "_prepare_llm_call", AsyncMock(return_value=100))
    with budget_scope(RequestBudget(100, quote=quote)), pytest.raises(RequestBudgetError):
        await LLMClient()._call_llm(
            [{"role": "user", "content": "Research"}],
            ["openrouter/example/primary", "openrouter/example/fallback"],
            [],
            broken_models=set(),
        )
    assert provider.await_count == 1


def test_thinking_tokens_share_the_capped_output_budget():
    units, bounded = openrouter_quote(
        {
            "model": "openrouter/example/model",
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 50},
        },
        [endpoint()],
    )
    assert units == 1200
    assert bounded["thinking"]["budget_tokens"] < bounded["max_tokens"]


@pytest.mark.asyncio
async def test_provider_overrun_is_recorded_and_stops_envelope():
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        with pytest.raises(RequestBudgetError, match="Provider charge"):
            await bounded_completion(
                AsyncMock(return_value=response("0.000070")), model="example/model"
            )
        assert budget.charged_units == 70
        with pytest.raises(RequestBudgetError):
            await bounded_completion(AsyncMock(), model="example/model")


def test_persisted_run_cost_includes_reserved_unknown_auxiliary_work(monkeypatch):
    from unittest.mock import Mock

    from robothor.engine.models import AgentRun
    from robothor.engine.run_finalizer import RunFinalizationMixin

    persist = Mock()
    monkeypatch.setattr("robothor.engine.run_finalizer.update_run", persist)
    finalizer = RunFinalizationMixin()
    monkeypatch.setattr(finalizer, "_assess_outcome", lambda run: None)
    monkeypatch.setattr(finalizer, "_update_task_for_run", lambda run: None)
    run = AgentRun(agent_id="research-agent", total_cost_usd=0.00001)
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        budget.reserve(60)
        finalizer._persist_run_sync(run)
    assert persist.call_args.kwargs["total_cost_usd"] == 0.00006


@pytest.mark.asyncio
async def test_pricing_fetch_is_fixed_origin_cached_and_never_uses_stale_data_on_error(monkeypatch):
    import httpx

    from robothor.engine.request_budget import OpenRouterQuotes

    calls = []
    fail = False

    def transport(request):
        calls.append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/models/example/model/endpoints"
        assert "authorization" not in request.headers
        if fail:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"data": {"id": "example/model", "endpoints": [endpoint()]}}
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs),
    )
    pricing = OpenRouterQuotes()
    kwargs = {"model": "openrouter/example/model", "max_tokens": 100}
    assert (await pricing(kwargs))[0] == 1200
    assert (await pricing(kwargs))[0] == 1200
    assert len(calls) == 1
    fail = True
    pricing.cache[kwargs["model"]] = (-1e9, [endpoint()])
    with pytest.raises(RequestBudgetError):
        await pricing(kwargs)
    assert len(calls) == 2
    with pytest.raises(RequestBudgetError):
        await pricing({"model": "openrouter/example/../../other"})
    assert len(calls) == 2


@pytest.mark.parametrize(
    "pricing",
    [
        {"prompt": "0.000001", "completion": "0.000002", "overrides": [{"completion": "1"}]},
        {"prompt": "0.000001", "completion": "0.000002", "input_cache_write": "0.000005"},
        {"prompt": "0.000001", "completion": "0.000002", "web_search": "0.005"},
    ],
)
def test_unpriced_surcharges_cannot_be_used_as_cheap_endpoints(pricing):
    with pytest.raises(RequestBudgetError):
        openrouter_quote(
            {"model": "openrouter/example/model", "max_tokens": 100}, [endpoint(pricing=pricing)]
        )
