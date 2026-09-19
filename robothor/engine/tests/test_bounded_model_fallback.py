"""Route exhaustion advances the declared chain; spending failures never do."""

from unittest.mock import AsyncMock

import litellm
import pytest

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.request_budget import (
    RequestBudget,
    RequestBudgetError,
    budget_scope,
    openrouter_quote,
)
from robothor.engine.tests.test_request_budget import endpoint
from robothor.engine.tests.test_request_routes import ProviderError

PRIMARY = "openrouter/example/primary"
FALLBACK = "openrouter/example/fallback"


@pytest.fixture
def dispatch(monkeypatch):
    breaker = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: breaker)
    monkeypatch.setattr(LLMClient, "_key_pool", lambda *args: None)
    monkeypatch.setattr(LLMClient, "_prepare_llm_call", AsyncMock(return_value=100))
    monkeypatch.setattr(llm_client, "note_outcome", lambda *args, **kwargs: None)
    seen = []

    async def provider(**kwargs):
        seen.append(kwargs)
        if kwargs.get("stream"):

            async def chunks():
                yield litellm.ModelResponse(
                    model=kwargs["model"],
                    stream=True,
                    choices=[
                        {
                            "delta": {"role": "assistant", "content": "Verified result"},
                            "finish_reason": "stop",
                        }
                    ],
                    usage={
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                        "cost": 0.000020,
                    },
                )

            return chunks()
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"content": "Verified result"}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.000020},
        )

    monkeypatch.setattr(litellm, "acompletion", provider)
    return seen, breaker


async def invoke(path):
    messages = [{"role": "user", "content": "Research a business"}]
    if path == "auxiliary":
        return await llm_client.llm_call(messages, model=[PRIMARY, FALLBACK], max_tokens=100)
    client = LLMClient()
    method = client._call_llm_streaming if path == "streaming" else client._call_llm
    return await method(messages, [PRIMARY, FALLBACK], [], broken_models=set())


def funded_quote(quoted):
    async def quote(kwargs):
        quoted.append(kwargs["model"])
        return openrouter_quote(kwargs, [endpoint(max_completion_tokens=100000)])

    return quote


@pytest.mark.parametrize("path", ["main", "streaming", "auxiliary"])
async def test_shared_worker_route_exclusion_reaches_funded_fallback(dispatch, path):
    seen, breaker = dispatch
    quoted = []
    budget = RequestBudget(500000, quote=funded_quote(quoted))
    # Another worker already failed on this model's only endpoint in this run.
    budget.routes.failed((PRIMARY, "example/fp8"), ProviderError(429))
    with budget_scope(budget):
        result = await invoke(path)
    assert result.choices[0].message.content == "Verified result"
    assert quoted == [PRIMARY, FALLBACK]
    assert [call["model"] for call in seen] == [FALLBACK]
    assert seen[0]["extra_body"]["provider"]["only"] == ["example/fp8"]
    assert seen[0]["extra_body"]["provider"]["allow_fallbacks"] is False
    assert seen[0]["num_retries"] == 0
    assert budget.charged_units == 20
    assert not breaker.is_open(PRIMARY)


@pytest.mark.parametrize("path", ["main", "streaming", "auxiliary"])
async def test_fallback_still_needs_remaining_funding(dispatch, path):
    seen, _ = dispatch
    quoted = []
    budget = RequestBudget(1, quote=funded_quote(quoted))
    budget.routes.failed((PRIMARY, "example/fp8"), ProviderError(429))
    with budget_scope(budget), pytest.raises(RequestBudgetError, match="allowance"):
        await invoke(path)
    assert quoted == [PRIMARY, FALLBACK]
    assert seen == []
    assert budget.charged_units == 0


@pytest.mark.parametrize("path", ["main", "streaming", "auxiliary"])
async def test_unpriced_request_features_remain_fatal(dispatch, path):
    seen, _ = dispatch
    quoted = []

    async def quote(kwargs):
        quoted.append(kwargs["model"])
        return openrouter_quote({**kwargs, "plugins": [{"id": "web"}]}, [endpoint()])

    with budget_scope(RequestBudget(500000, quote=quote)), pytest.raises(RequestBudgetError):
        await invoke(path)
    assert quoted == [PRIMARY]
    assert seen == []
