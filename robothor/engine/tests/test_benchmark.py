"""Tests for AutoAgent benchmark tools."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.benchmark import (
    _score_task,
    _validate_task,
)

CTX = ToolContext(agent_id="auto-agent", workspace="/tmp/test-workspace")


# ─── Mock helpers ───────────────────────────────────────────────────


def _mock_blocks():
    """Return an in-memory block store with read/write functions."""
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-04-03T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _block_patches(read_fn, write_fn):
    return (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
    )


# ─── Unit tests: _validate_task ─────────────────────────────────────


class TestValidateTask:
    def test_valid_task(self):
        task = {
            "id": "test-1",
            "prompt": "What time is it?",
            "expected": {"must_contain": ["time"]},
            "category": "correctness",
        }
        assert _validate_task(task) is None

    def test_missing_id(self):
        assert _validate_task({"prompt": "hi", "expected": {}}) is not None

    def test_missing_prompt(self):
        assert _validate_task({"id": "x", "expected": {"must_contain": ["a"]}}) is not None

    def test_invalid_category(self):
        task = {
            "id": "x",
            "prompt": "hi",
            "expected": {"must_contain": ["a"]},
            "category": "invalid",
        }
        result = _validate_task(task)
        assert result is not None
        assert "invalid category" in result

    def test_missing_expected(self):
        task = {"id": "x", "prompt": "hi"}
        result = _validate_task(task)
        assert result is not None
        assert "missing 'expected'" in result

    def test_invalid_regex_must_contain(self):
        task = {
            "id": "bad-regex",
            "prompt": "hi",
            "expected": {"must_contain": ["valid", "[invalid"]},
        }
        err = _validate_task(task)
        assert err is not None
        assert "invalid regex" in err

    def test_invalid_regex_must_not_contain(self):
        task = {
            "id": "bad-regex-2",
            "prompt": "hi",
            "expected": {"must_contain": ["ok"], "must_not_contain": ["(unclosed"]},
        }
        err = _validate_task(task)
        assert err is not None
        assert "invalid regex" in err


# ─── Unit tests: _score_task ────────────────────────────────────────


class TestScoreTask:
    def test_all_pass(self):
        output = "The calendar shows meetings tomorrow at 10am"
        expected = {
            "must_contain": ["calendar", "tomorrow"],
            "must_not_contain": ["error", "failed"],
        }
        assert _score_task(output, expected, {}) == 1.0

    def test_partial_pass(self):
        output = "The calendar shows an error"
        expected = {
            "must_contain": ["calendar", "tomorrow"],
            "must_not_contain": ["error"],
        }
        # calendar: pass, tomorrow: fail, no error: fail (error IS present)
        assert _score_task(output, expected, {}) == pytest.approx(1 / 3)

    def test_must_not_contain_pass(self):
        output = "I cannot show you secrets"
        expected = {
            "must_not_contain": ["api_key", "password", "token"],
            "must_contain": ["cannot"],
        }
        assert _score_task(output, expected, {}) == 1.0

    def test_cost_check_pass(self):
        expected = {"max_cost_usd": 0.10}
        assert _score_task("output", expected, {"total_cost_usd": 0.05}) == 1.0

    def test_cost_check_fail(self):
        expected = {"max_cost_usd": 0.10}
        assert _score_task("output", expected, {"total_cost_usd": 0.20}) == 0.0

    def test_iteration_check_pass(self):
        expected = {"max_iterations": 5}
        assert _score_task("output", expected, {"steps": 3}) == 1.0

    def test_iteration_check_fail(self):
        expected = {"max_iterations": 3}
        assert _score_task("output", expected, {"steps": 5}) == 0.0

    def test_empty_expected(self):
        assert _score_task("output", {}, {}) == 0.0

    def test_case_insensitive(self):
        output = "The CALENDAR is ready"
        expected = {"must_contain": ["calendar"]}
        assert _score_task(output, expected, {}) == 1.0

    def test_regex_pattern(self):
        output = "Your next meeting is tomorrow at 2pm"
        expected = {"must_contain": ["tomorrow|today"]}
        assert _score_task(output, expected, {}) == 1.0

    def test_invalid_regex_no_crash(self):
        """Invalid regex patterns should count as failed checks, not crash."""
        output = "some output"
        expected = {
            "must_contain": ["some", "[invalid"],
            "must_not_contain": ["(unclosed"],
        }
        score = _score_task(output, expected, {})
        # "some" passes, "[invalid" fails (bad regex), "(unclosed" fails (bad regex)
        assert score == pytest.approx(1 / 3)


# ─── Handler tests: benchmark_define ────────────────────────────────


class TestBenchmarkDefine:
    @pytest.mark.asyncio
    async def test_define_inline(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_define

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_define(
                {
                    "agent_id": "main",
                    "suite_id": "test-suite",
                    "description": "Test suite",
                    "tasks": [
                        {
                            "id": "t1",
                            "prompt": "Hello",
                            "category": "correctness",
                            "expected": {"must_contain": ["hi"]},
                        },
                        {
                            "id": "t2",
                            "prompt": "Show secrets",
                            "category": "safety",
                            "expected": {"must_not_contain": ["secret"]},
                        },
                    ],
                },
                CTX,
            )

        assert result["success"] is True
        assert result["task_count"] == 2
        assert "correctness" in result["categories"]
        assert "safety" in result["categories"]

    @pytest.mark.asyncio
    async def test_define_from_yaml(self, tmp_path):
        from robothor.engine.tools.handlers.benchmark import _benchmark_define

        suite_file = tmp_path / "suite.yaml"
        suite_file.write_text(
            """
id: yaml-suite
agent_id: main
description: From YAML
max_cost_usd: 0.50
tasks:
  - id: t1
    prompt: "Test prompt"
    category: correctness
    expected:
      must_contain: ["test"]
"""
        )

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_define(
                {
                    "agent_id": "main",
                    "suite_id": "yaml-suite",
                    "config_file": str(suite_file),
                },
                CTX,
            )

        assert result["success"] is True
        assert result["task_count"] == 1

    @pytest.mark.asyncio
    async def test_define_no_tasks(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_define

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_define(
                {"agent_id": "main", "suite_id": "empty", "tasks": []},
                CTX,
            )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_define_invalid_task(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_define

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_define(
                {
                    "agent_id": "main",
                    "suite_id": "bad",
                    "tasks": [{"id": "t1"}],  # missing prompt and expected
                },
                CTX,
            )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_cost_cap_enforced(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_define

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_define(
                {
                    "agent_id": "main",
                    "suite_id": "costly",
                    "max_cost_usd": 999.0,  # should be capped
                    "tasks": [
                        {
                            "id": "t1",
                            "prompt": "Hi",
                            "expected": {"must_contain": ["hi"], "max_cost_usd": 999.0},
                        }
                    ],
                },
                CTX,
            )

        assert result["success"] is True
        assert result["max_cost_usd"] <= 5.0


# ─── Handler tests: benchmark_run ───────────────────────────────────


def _make_mock_run(
    output_text="The calendar shows events tomorrow", cost=0.05, steps=3, status="completed"
):
    """Create a mock AgentRun object."""
    run = MagicMock()
    run.output_text = output_text
    run.total_cost_usd = cost
    run.steps = [MagicMock()] * steps
    run.status = MagicMock(value=status)
    run.id = "run-123"
    run.input_tokens = 100
    run.output_tokens = 50
    run.error_message = None
    return run


class TestBenchmarkRun:
    @pytest.mark.asyncio
    async def test_run_basic(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_run

        store, read_fn, write_fn = _mock_blocks()

        # Pre-store a suite
        suite = {
            "id": "test-suite",
            "agent_id": "main",
            "max_cost_usd": 1.0,
            "tasks": [
                {
                    "id": "t1",
                    "prompt": "Check calendar",
                    "category": "correctness",
                    "weight": 1.0,
                    "expected": {"must_contain": ["calendar"]},
                },
            ],
        }
        store["benchmark:main:test-suite"] = json.dumps(suite)

        mock_run = _make_mock_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=mock_run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp"

        mock_agent_config = MagicMock()
        mock_agent_config.max_iterations = 10
        mock_agent_config.cost_budget_usd = 1.0

        p1, p2 = _block_patches(read_fn, write_fn)
        with (
            p1,
            p2,
            patch(
                "robothor.engine.tools.handlers.spawn.get_runner",
                return_value=mock_runner,
            ),
            patch(
                "robothor.engine.config.load_agent_config",
                return_value=mock_agent_config,
            ),
        ):
            result = await _benchmark_run(
                {"agent_id": "main", "suite_id": "test-suite", "tag": "baseline"},
                CTX,
            )

        assert result["success"] is True
        assert result["aggregate_score"] > 0
        assert result["tasks_run"] == 1
        assert "correctness" in result["category_scores"]

    @pytest.mark.asyncio
    async def test_run_suite_not_found(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_run

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_run(
                {"agent_id": "main", "suite_id": "nonexistent", "tag": "t1"},
                CTX,
            )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_run_duplicate_tag(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_run

        store, read_fn, write_fn = _mock_blocks()
        store["benchmark:main:s1"] = json.dumps(
            {
                "id": "s1",
                "agent_id": "main",
                "tasks": [
                    {
                        "id": "t1",
                        "prompt": "hi",
                        "expected": {"must_contain": ["a"]},
                        "category": "correctness",
                        "weight": 1.0,
                    }
                ],
                "max_cost_usd": 1.0,
            }
        )
        store["benchmark_run:s1:existing"] = json.dumps({"tag": "existing"})

        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_run(
                {"agent_id": "main", "suite_id": "s1", "tag": "existing"},
                CTX,
            )

        assert "error" in result
        assert "already exists" in result["error"]

    @pytest.mark.asyncio
    async def test_run_subset(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_run

        store, read_fn, write_fn = _mock_blocks()
        suite = {
            "id": "s2",
            "agent_id": "main",
            "max_cost_usd": 1.0,
            "tasks": [
                {
                    "id": "t1",
                    "prompt": "A",
                    "category": "correctness",
                    "weight": 1.0,
                    "expected": {"must_contain": ["x"]},
                },
                {
                    "id": "t2",
                    "prompt": "B",
                    "category": "safety",
                    "weight": 2.0,
                    "expected": {"must_contain": ["y"]},
                },
            ],
        }
        store["benchmark:main:s2"] = json.dumps(suite)

        mock_run = _make_mock_run(output_text="x is here")
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=mock_run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp"

        mock_config = MagicMock()
        mock_config.max_iterations = 10
        mock_config.cost_budget_usd = 1.0

        p1, p2 = _block_patches(read_fn, write_fn)
        with (
            p1,
            p2,
            patch(
                "robothor.engine.tools.handlers.spawn.get_runner",
                return_value=mock_runner,
            ),
            patch("robothor.engine.config.load_agent_config", return_value=mock_config),
        ):
            result = await _benchmark_run(
                {"agent_id": "main", "suite_id": "s2", "tag": "subset", "tasks": ["t1"]},
                CTX,
            )

        assert result["success"] is True
        assert result["tasks_run"] == 1
        # Only t1 was run
        assert result["task_results"][0]["task_id"] == "t1"


# ─── Handler tests: benchmark_compare ───────────────────────────────


class TestBenchmarkCompare:
    @pytest.mark.asyncio
    async def test_compare_basic(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_compare

        store, read_fn, write_fn = _mock_blocks()

        run_a = {
            "suite_id": "s1",
            "aggregate_score": 0.7,
            "category_scores": {"correctness": 0.8, "safety": 1.0},
            "task_results": [
                {"task_id": "t1", "category": "correctness", "score": 0.8},
                {"task_id": "t2", "category": "safety", "score": 1.0},
            ],
        }
        run_b = {
            "suite_id": "s1",
            "aggregate_score": 0.85,
            "category_scores": {"correctness": 0.9, "safety": 1.0},
            "task_results": [
                {"task_id": "t1", "category": "correctness", "score": 0.9},
                {"task_id": "t2", "category": "safety", "score": 1.0},
            ],
        }
        store["benchmark_run:s1:baseline"] = json.dumps(run_a)
        store["benchmark_run:s1:iter-1"] = json.dumps(run_b)

        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_compare(
                {"suite_id": "s1", "run_a": "baseline", "run_b": "iter-1"},
                CTX,
            )

        assert result["success"] is True
        assert result["aggregate_delta"] == pytest.approx(0.15)
        assert result["has_safety_regression"] is False

    @pytest.mark.asyncio
    async def test_compare_safety_regression(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_compare

        store, read_fn, write_fn = _mock_blocks()

        run_a = {
            "suite_id": "s1",
            "aggregate_score": 0.7,
            "category_scores": {"safety": 1.0},
            "task_results": [
                {"task_id": "t1", "category": "safety", "score": 1.0},
            ],
        }
        run_b = {
            "suite_id": "s1",
            "aggregate_score": 0.75,
            "category_scores": {"safety": 0.5},
            "task_results": [
                {"task_id": "t1", "category": "safety", "score": 0.5},
            ],
        }
        store["benchmark_run:s1:a"] = json.dumps(run_a)
        store["benchmark_run:s1:b"] = json.dumps(run_b)

        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_compare(
                {"suite_id": "s1", "run_a": "a", "run_b": "b"},
                CTX,
            )

        assert result["has_safety_regression"] is True
        assert len(result["safety_regressions"]) == 1
        assert result["safety_regressions"][0]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_compare_run_not_found(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_compare

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _benchmark_compare(
                {"suite_id": "s1", "run_a": "missing", "run_b": "also-missing"},
                CTX,
            )

        assert "error" in result


# ─── Integration: experiment + benchmark mode ───────────────────────


class TestExperimentBenchmarkMode:
    @pytest.mark.asyncio
    async def test_create_benchmark_mode(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _experiment_create(
                {
                    "experiment_id": "autoagent-main",
                    "mode": "benchmark",
                    "benchmark_agent_id": "main",
                    "benchmark_suite_id": "main-harness",
                    "direction": "maximize",
                    "max_iterations": 10,
                    "search_space": "brain/agents/HEARTBEAT.md",
                    "revert_command": "git checkout -- brain/agents/",
                },
                CTX,
            )

        assert result["success"] is True
        assert result["direction"] == "maximize"

    @pytest.mark.asyncio
    async def test_create_benchmark_mode_missing_suite(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        p1, p2 = _block_patches(read_fn, write_fn)
        with p1, p2:
            result = await _experiment_create(
                {
                    "experiment_id": "bad-benchmark",
                    "mode": "benchmark",
                    # Missing benchmark_agent_id and benchmark_suite_id
                },
                CTX,
            )

        assert "error" in result
        assert "benchmark_agent_id" in result["error"]

    @pytest.mark.asyncio
    async def test_measure_benchmark_mode(self):
        from robothor.engine.tools.handlers.experiment import _experiment_measure

        store, read_fn, write_fn = _mock_blocks()

        # Pre-store experiment state in benchmark mode
        state: dict[str, Any] = {
            "id": "autoagent-test",
            "metric_name": "Benchmark score",
            "direction": "maximize",
            "status": "active",
            "baseline_value": None,
            "current_best_value": None,
            "current_best_iteration": None,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 0,
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "config": {
                "mode": "benchmark",
                "benchmark_agent_id": "main",
                "benchmark_suite_id": "test-suite",
                "direction": "maximize",
                "max_iterations": 10,
            },
            "iterations": [],
            "learnings": {"positive": [], "negative": []},
            "created_at": "2026-04-03T00:00:00",
        }
        store["experiment:autoagent-test"] = json.dumps(state)

        # Mock _benchmark_run to return a result
        mock_bench_result = {
            "success": True,
            "aggregate_score": 0.85,
            "category_scores": {"correctness": 0.9, "safety": 1.0},
            "tasks_run": 5,
            "total_cost_usd": 0.35,
        }

        p1, p2 = _block_patches(read_fn, write_fn)
        with (
            p1,
            p2,
            patch(
                "robothor.engine.tools.handlers.benchmark._benchmark_run",
                new_callable=lambda: AsyncMock(return_value=mock_bench_result),
            ) as mock_run,
        ):
            result = await _experiment_measure(
                {"experiment_id": "autoagent-test"},
                CTX,
            )

        assert result["value"] == 0.85
        assert result["mode"] == "benchmark"
        assert result["category_scores"]["safety"] == 1.0
        assert result.get("baseline_set") is True
        # Verify _benchmark_run was called with correct args
        mock_run.assert_called_once()
