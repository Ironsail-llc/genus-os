"""A funded benchmark invocation must include agent and semantic judge requests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.request_budget import (
    RequestBudget,
    RequestBudgetError,
    active_budget,
    bounded_completion,
    budget_scope,
)
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import benchmark, spawn


@pytest.fixture
def entry(monkeypatch, tmp_path):
    suite = {
        "id": "fixture",
        "agent_id": "example",
        "hard_request_budget": True,
        "max_cost_usd": 0.0001,
        "tasks": [{"id": "one", "prompt": "Return JSON", "expected": {"must_contain": ["yes"]}}],
    }
    blocks = {}
    monkeypatch.setattr(
        benchmark, "_load_block", lambda key: suite if key == "benchmark:example:fixture" else None
    )
    monkeypatch.setattr(benchmark, "_save_block", lambda key, value: blocks.update({key: value}))
    monkeypatch.setattr(benchmark, "_write_benchmark_result_row", lambda **kwargs: None)
    monkeypatch.setattr(benchmark, "sandbox_active", lambda: False)
    monkeypatch.setattr(spawn, "get_runner", lambda: SimpleNamespace())

    async def quote(self, kwargs):
        return 60, {**kwargs, "num_retries": 0}

    monkeypatch.setattr("robothor.engine.request_budget.OpenRouterQuotes.__call__", quote)
    ctx = ToolContext(agent_id="benchmark-runner", workspace=str(tmp_path))
    return suite, blocks, ctx


async def test_entry_funds_requests_and_records_total_including_judge(entry, monkeypatch):
    _, blocks, ctx = entry
    provider = AsyncMock(return_value=SimpleNamespace(usage={"cost": "0.000040"}))

    async def execute(**kwargs):
        assert active_budget() is not None
        await bounded_completion(provider, model="example/agent")
        await bounded_completion(provider, model="example/judge")
        with pytest.raises(RequestBudgetError):
            await bounded_completion(provider, model="example/retry")
        return [
            {
                "task_id": "one",
                "score": 1.0,
                "category": "correctness",
                "weight": 1,
                "outcome": "scored",
            }
        ], 0.000040

    monkeypatch.setattr(benchmark, "_execute_suite_tasks", execute)
    result = await benchmark._benchmark_run(
        {"agent_id": "example", "suite_id": "fixture", "tag": "run-1"}, ctx
    )
    assert result["success"]
    record = blocks["benchmark_run:fixture:run-1"]
    assert record["request_budget"] == {
        "limit_units": 100,
        "charged_units": 80,
        "accounting": "actual_or_reserved_unknown",
    }
    assert record["total_cost_usd"] == 0.000080
    assert result["request_budget"] == record["request_budget"]
    assert result["total_cost_usd"] == 0.000080
    assert active_budget() is None
    assert provider.await_count == 2


@pytest.mark.parametrize("limit", [True, -1, float("nan"), float("inf"), 0.0000001])
async def test_invalid_funded_limits_refused_before_execution(entry, monkeypatch, limit):
    suite, _, ctx = entry
    suite["max_cost_usd"] = limit
    execute = AsyncMock()
    monkeypatch.setattr(benchmark, "_execute_suite_tasks", execute)
    result = await benchmark._benchmark_run(
        {"agent_id": "example", "suite_id": "fixture", "tag": "run-1"}, ctx
    )
    assert result.get("error")
    execute.assert_not_awaited()


async def test_an_existing_funded_scope_cannot_be_reset(entry, monkeypatch):
    _, _, ctx = entry
    execute = AsyncMock()
    monkeypatch.setattr(benchmark, "_execute_suite_tasks", execute)
    with budget_scope(RequestBudget(50)):
        result = await benchmark._benchmark_run(
            {"agent_id": "example", "suite_id": "fixture", "tag": "run-1"}, ctx
        )
        assert result.get("error")
        assert active_budget().limit_units == 50
    execute.assert_not_awaited()


async def test_task_loop_accounts_for_judge_and_refuses_unfunded_next_request(entry, monkeypatch):
    import litellm

    from robothor.engine import config, key_pool
    from robothor.engine.models import RunStatus

    suite, blocks, ctx = entry
    suite["tasks"][0]["expected"]["judge"] = {"rubric": ["Says yes"], "threshold": 1.0}
    suite["tasks"].append({"id": "two", "prompt": "Again", "expected": {"must_contain": ["yes"]}})
    provider = AsyncMock(
        return_value=SimpleNamespace(
            usage={"cost": "0.000040"},
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"scores":[1]}'))],
        )
    )
    monkeypatch.setattr(litellm, "acompletion", provider)
    monkeypatch.setattr(key_pool, "api_key_for_model", lambda _: None)
    monkeypatch.setattr(
        spawn,
        "get_runner",
        lambda: SimpleNamespace(config=SimpleNamespace(manifest_dir=ctx.workspace)),
    )
    monkeypatch.setattr(
        config, "load_agent_config_or_reason", lambda *args: (SimpleNamespace(), None)
    )
    monkeypatch.setattr(benchmark, "_shape_child_config", lambda *args: None)
    monkeypatch.setattr(benchmark, "_agent_task_cost_ceiling", lambda *args: 1.0)

    async def execute(**kwargs):
        await bounded_completion(provider, model="example/agent")
        return SimpleNamespace(
            output_text="yes", total_cost_usd=0.000040, steps=[], status=RunStatus.COMPLETED
        )

    monkeypatch.setattr(benchmark, "_execute_task_run", execute)
    result = await benchmark._benchmark_run(
        {"agent_id": "example", "suite_id": "fixture", "tag": "run-1"}, ctx
    )
    assert result["passed"] == 1 and result["failed"] == 1
    assert provider.await_count == 2
    rows = blocks["benchmark_run:fixture:run-1"]["task_results"]
    assert rows[0]["charged_units"] == 80 and rows[0]["cost_usd"] == 0.000080
    assert rows[1]["charged_units"] == 0 and rows[1]["outcome"] == "error"
    assert result["total_cost_usd"] == 0.000080


@pytest.mark.parametrize("flag,limit", [("true", 1), (True, True), (True, float("nan"))])
async def test_disk_definition_rejects_invalid_funding_before_normalizing(
    entry, monkeypatch, flag, limit
):
    from pathlib import Path

    import yaml

    _, blocks, ctx = entry
    directory = Path(ctx.workspace) / "docs/benchmarks/example"
    directory.mkdir(parents=True)
    (directory / "suite.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "fixture",
                "hard_request_budget": flag,
                "max_cost_usd": limit,
                "tasks": [
                    {"id": "one", "prompt": "Hello", "expected": {"must_contain": ["hello"]}}
                ],
            }
        )
    )
    result = await benchmark.auto_define_suite_from_disk("example", ctx.workspace)
    assert result.get("error")
    assert not blocks
