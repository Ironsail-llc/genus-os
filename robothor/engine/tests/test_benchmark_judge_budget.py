"""Semantic judge attempts share a funded request envelope when one is active."""

import asyncio
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


async def test_judge_decisions_are_retained_for_failed_case_diagnosis(monkeypatch):
    setup(monkeypatch, response('{"scores":[1,0]}'))
    score, detail = await benchmark._score_task_detailed(
        '{"criteria":{}}',
        {"require_all": True, "judge": {"rubric": ["first", "second"], "threshold": 1}},
        {},
        [],
    )
    assert score == 0
    assert detail["judge"] == {
        "model": "openrouter/xiaomi/mimo-v2.5-pro",
        "threshold": 1.0,
        "score": 0.5,
        "item_scores": [1, 0],
    }


async def test_failed_judge_does_not_invent_item_decisions(monkeypatch):
    setup(monkeypatch, response('{"scores":[true]}'))
    score, detail = await benchmark._score_task_detailed(
        "{}", {"judge": {"rubric": ["criterion"]}}, {}, []
    )
    assert score == 0
    assert detail["judge"]["score"] is None
    assert detail["judge"]["item_scores"] == []
    assert detail["judge_error"]


async def test_judge_wall_clock_deadline_bounds_a_provider_that_ignores_timeout(monkeypatch):
    call = setup(monkeypatch, None)

    async def stuck(**kwargs):
        await asyncio.Event().wait()

    call.side_effect = stuck
    monkeypatch.setattr(benchmark, "JUDGE_REQUEST_TIMEOUT_SECONDS", 0.01, raising=False)
    budget = RequestBudget(100, quote=quote)
    with budget_scope(budget):
        result = await asyncio.wait_for(
            benchmark._judge_output("output", ["criterion"], "example/model"), 0.3
        )
    assert result.score is None and result.error
    assert call.await_count == 1
    assert budget.charged_units == 60


@pytest.mark.parametrize("score", ['"0"', '"false"', "2", "true", "0.5", "null"])
async def test_nonbinary_judge_scores_are_errors_not_truthy_passes(monkeypatch, score):
    setup(monkeypatch, response('{"scores":[' + score + "]}"))
    result = await benchmark._judge_output("output", ["criterion"], "example/model")
    assert result.score is None and result.error
