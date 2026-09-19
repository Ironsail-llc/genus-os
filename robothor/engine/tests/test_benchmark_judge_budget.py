"""Semantic judge attempts share a funded request envelope when one is active."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.request_budget import RequestBudget, budget_scope
from robothor.engine.tools.handlers import benchmark


def response(content, cost=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage={"cost": cost},
    )


def setup(monkeypatch, result):
    import litellm

    from robothor.engine import key_pool

    call = AsyncMock(return_value=result)
    monkeypatch.setattr(litellm, "acompletion", call)
    monkeypatch.setattr(key_pool, "api_key_for_model", lambda _: None)
    monkeypatch.setattr(benchmark, "JUDGE_RETRY_DELAY_S", 0)
    return call


async def quote(kwargs):
    return 60, {**kwargs, "num_retries": 0}


async def test_missing_usage_and_empty_judge_output_cannot_retry_outside_budget(monkeypatch):
    call = setup(monkeypatch, response(""))
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        result = await benchmark._judge_output("output", ["criterion"], "example/model")
    assert result.score is None and result.error
    assert call.await_count == 1
    assert budget.charged_units == 60


async def test_successful_judge_settles_actual_cost(monkeypatch):
    setup(monkeypatch, response('{"scores":[1]}', "0.000020"))
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        result = await benchmark._judge_output("output", ["criterion"], "example/model")
    assert result.score == 1 and budget.charged_units == 20


@pytest.mark.parametrize("score", ['"0"', '"false"', "2", "true", "0.5", "null"])
async def test_nonbinary_judge_scores_are_errors_not_truthy_passes(monkeypatch, score):
    setup(monkeypatch, response('{"scores":[' + score + "]}"))
    result = await benchmark._judge_output("output", ["criterion"], "example/model")
    assert result.score is None and result.error
