"""Funded retries change failed routes without changing authority or spending caps."""

import pytest

from robothor.engine.request_budget import (
    RequestBudget,
    RequestBudgetError,
    bounded_completion,
    budget_scope,
    openrouter_quote,
)
from robothor.engine.tests.test_request_budget import endpoint, response


class ProviderError(Exception):
    def __init__(self, status):
        self.status_code = status


async def quotes(kwargs):
    return openrouter_quote(
        kwargs,
        [
            endpoint(tag="first/fp8"),
            endpoint(tag="second/fp8", pricing={"prompt": "0.000002", "completion": "0.000003"}),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_failed_endpoint_is_excluded_on_next_attempt_without_refunding_unknown_cost(status):
    seen = []

    async def provider(**kwargs):
        seen.append(kwargs["extra_body"]["provider"])
        if len(seen) == 1:
            raise ProviderError(status)
        return response("0.000020")

    budget = RequestBudget(10000, quote=quotes)
    with budget_scope(budget):
        with pytest.raises(ProviderError):
            await bounded_completion(provider, model="openrouter/test/model", max_tokens=100)
        await bounded_completion(provider, model="openrouter/test/model", max_tokens=100)
    assert [r["only"] for r in seen] == [["first/fp8"], ["second/fp8"]]
    assert all(r["allow_fallbacks"] is False for r in seen)
    assert budget.charged_units == 1220


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routing", [{"only": ["first"]}, {"max_price": {"prompt": 1}}, {"ignore": ["second"]}]
)
async def test_endpoint_failure_never_relaxes_explicit_provider_constraints(routing):
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        raise ProviderError(429)

    budget = RequestBudget(10000, quote=quotes)
    kwargs = {
        "model": "openrouter/test/model",
        "max_tokens": 100,
        "extra_body": {"provider": routing},
    }
    with budget_scope(budget):
        with pytest.raises(ProviderError):
            await bounded_completion(provider, **kwargs)
        with pytest.raises(RequestBudgetError):
            await bounded_completion(provider, **kwargs)
    assert len(calls) == 1
    assert kwargs["extra_body"]["provider"] == routing
    assert budget.charged_units == 1200


@pytest.mark.asyncio
async def test_more_expensive_alternative_still_needs_remaining_funding():
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        raise ProviderError(429)

    budget = RequestBudget(2000, quote=quotes)
    with budget_scope(budget):
        with pytest.raises(ProviderError):
            await bounded_completion(provider, model="openrouter/test/model", max_tokens=100)
        with pytest.raises(RequestBudgetError):
            await bounded_completion(provider, model="openrouter/test/model", max_tokens=100)
    assert len(calls) == 1
    assert budget.charged_units == 1200


@pytest.mark.asyncio
async def test_auth_error_does_not_change_route_and_other_model_stays_independent():
    seen = []

    async def provider(**kwargs):
        seen.append(kwargs["extra_body"]["provider"]["only"])
        raise ProviderError(401 if len(seen) == 1 else 429)

    with budget_scope(RequestBudget(10000, quote=quotes)):
        for model in ("openrouter/test/one", "openrouter/test/one", "openrouter/test/two"):
            with pytest.raises(ProviderError):
                await bounded_completion(provider, model=model, max_tokens=100)
    assert seen == [["first/fp8"]] * 3


@pytest.mark.asyncio
async def test_stream_failure_excludes_route_but_cancellation_does_not():
    import asyncio

    seen = []
    closed = []

    class Stream:
        def __init__(self, error):
            self.error = error

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise self.error

        async def aclose(self):
            closed.append(True)

    async def provider(**kwargs):
        seen.append(kwargs["extra_body"]["provider"]["only"])
        return Stream(ProviderError(503) if len(seen) == 1 else asyncio.CancelledError())

    with budget_scope(RequestBudget(10000, quote=quotes)) as budget:
        stream = await bounded_completion(
            provider, model="openrouter/test/model", max_tokens=100, stream=True
        )
        with pytest.raises(ProviderError):
            await anext(stream)
        for _ in range(2):
            stream = await bounded_completion(
                provider, model="openrouter/test/model", max_tokens=100, stream=True
            )
            with pytest.raises(asyncio.CancelledError):
                await anext(stream)
    assert seen == [["first/fp8"], ["second/fp8"], ["second/fp8"]]
    assert len(closed) == 3
    assert budget.charged_units == 5800


@pytest.mark.asyncio
async def test_new_funded_run_does_not_inherit_endpoint_failures():
    seen = []

    async def provider(**kwargs):
        seen.append(kwargs["extra_body"]["provider"]["only"])
        raise ProviderError(429)

    for _ in range(2):
        with budget_scope(RequestBudget(10000, quote=quotes)):
            with pytest.raises(ProviderError):
                await bounded_completion(provider, model="openrouter/test/model", max_tokens=100)
    assert seen == [["first/fp8"], ["first/fp8"]]
