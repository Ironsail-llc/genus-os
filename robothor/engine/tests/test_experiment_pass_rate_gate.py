"""Tests for the 2026-05-06 Auto Researcher rebuild guards.

Verifies that experiment_create rejects metric-mode experiments without
operator override, and that experiment_commit force-reverts when a
benchmark case that passed at baseline is now failing.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

CTX = ToolContext(agent_id="auto-researcher", workspace="/tmp/test-workspace")


def _mock_blocks(initial: dict[str, str] | None = None):
    store: dict[str, str] = dict(initial or {})

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-05-06T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


# ─── experiment_create — metric-mode now requires operator_override ──


class TestExperimentCreateGuard:
    @pytest.mark.asyncio
    async def test_metric_mode_without_override_is_rejected(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "rejected",
                    "metric_command": "echo 1",
                    "direction": "minimize",
                    "mode": "metric",
                },
                CTX,
            )
        assert result.get("operator_override_required") is True
        assert "benchmark" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_metric_mode_with_override_passes(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "approved",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "mode": "metric",
                    "operator_override": "operator approved emergency",
                },
                CTX,
            )
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_benchmark_mode_with_forbidden_metric_name_is_rejected(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "trojan-cost",
                    "metric_name": "Reduce p95 cost via suite",
                    "mode": "benchmark",
                    "benchmark_agent_id": "main",
                    "benchmark_suite_id": "main-harness",
                },
                CTX,
            )
        assert "error" in result
        assert "forbidden" in result["error"]

    @pytest.mark.asyncio
    async def test_benchmark_mode_clean_passes(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "clean-bench",
                    "metric_name": "email-classifier urgency detection",
                    "mode": "benchmark",
                    "benchmark_agent_id": "email-classifier",
                    "benchmark_suite_id": "email-classifier-harness",
                },
                CTX,
            )
        assert result.get("success") is True


# ─── experiment_commit — per-case regression force-revert ────────────


class TestExperimentCommitRegressionGuard:
    @pytest.mark.asyncio
    async def test_commit_keep_reverts_when_baseline_case_now_fails(self):
        """A case scoring 1.0 at baseline and 0.0 in the latest run must trigger revert."""
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        # Pre-existing experiment state in benchmark mode
        exp_id = "regress-test"
        suite_id = "main-harness"
        baseline_run = {
            "task_results": [
                {"task_id": "case-A", "score": 1.0, "weight": 1.0, "category": "correctness"},
                {"task_id": "case-B", "score": 1.0, "weight": 1.0, "category": "correctness"},
            ]
        }
        # Iteration 2 (current_iter): case-A passes, case-B regresses to fail
        current_run = {
            "task_results": [
                {"task_id": "case-A", "score": 1.0, "weight": 1.0, "category": "correctness"},
                {"task_id": "case-B", "score": 0.0, "weight": 1.0, "category": "correctness"},
            ]
        }
        state = {
            "id": exp_id,
            "metric_name": "main",
            "direction": "maximize",
            "status": "active",
            "created_at": "2026-05-06T00:00:00+00:00",
            "updated_at": "2026-05-06T00:00:00+00:00",
            "baseline_value": 1.0,
            "current_best_value": 1.0,
            "current_best_iteration": 1,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 1,  # current run is iter-2
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "iterations": [],
            "learnings": {"positive": [], "negative": []},
            "config": {
                "mode": "benchmark",
                "benchmark_agent_id": "main",
                "benchmark_suite_id": suite_id,
                "search_space": "",
                "guardrails": [],
            },
        }

        store, read_fn, write_fn = _mock_blocks(
            initial={
                f"experiment:{exp_id}": json.dumps(state),
                f"benchmark_run:{suite_id}:exp-{exp_id}-iter-1": json.dumps(baseline_run),
                f"benchmark_run:{suite_id}:exp-{exp_id}-iter-2": json.dumps(current_run),
            }
        )

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": exp_id,
                    "hypothesis": "tweak the prompt",
                    "changes": [{"file": "x.md", "description": "removed urgency rules"}],
                    "metric_before": 1.0,
                    "metric_after": 0.5,
                    "verdict": "keep",
                    "learnings": "removing rules should help",
                    "cost_usd": 0.01,
                },
                CTX,
            )

        # The force-revert should have flipped verdict to revert
        assert result.get("verdict") == "revert" or "regress" in str(result).lower()
        # State persisted should reflect revert
        new_state = json.loads(store[f"experiment:{exp_id}"])
        last_iter = new_state["iterations"][-1]
        assert last_iter["verdict"] == "revert"

    @pytest.mark.asyncio
    async def test_commit_keep_passes_when_no_case_regressed(self):
        """Same setup but no case regressed — keep should stand."""
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        exp_id = "no-regress"
        suite_id = "main-harness"
        baseline_run = {
            "task_results": [
                {"task_id": "case-A", "score": 0.7, "weight": 1.0, "category": "correctness"},
                {"task_id": "case-B", "score": 0.3, "weight": 1.0, "category": "correctness"},
            ]
        }
        current_run = {
            "task_results": [
                {"task_id": "case-A", "score": 0.9, "weight": 1.0, "category": "correctness"},
                {"task_id": "case-B", "score": 0.5, "weight": 1.0, "category": "correctness"},
            ]
        }
        state = {
            "id": exp_id,
            "metric_name": "main",
            "direction": "maximize",
            "status": "active",
            "created_at": "2026-05-06T00:00:00+00:00",
            "updated_at": "2026-05-06T00:00:00+00:00",
            "baseline_value": 0.5,
            "current_best_value": 0.5,
            "current_best_iteration": 1,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 1,
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "iterations": [],
            "learnings": {"positive": [], "negative": []},
            "config": {
                "mode": "benchmark",
                "benchmark_agent_id": "main",
                "benchmark_suite_id": suite_id,
                "search_space": "",
                "guardrails": [],
            },
        }

        store, read_fn, write_fn = _mock_blocks(
            initial={
                f"experiment:{exp_id}": json.dumps(state),
                f"benchmark_run:{suite_id}:exp-{exp_id}-iter-1": json.dumps(baseline_run),
                f"benchmark_run:{suite_id}:exp-{exp_id}-iter-2": json.dumps(current_run),
            }
        )

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            await _experiment_commit(
                {
                    "experiment_id": exp_id,
                    "hypothesis": "tweak X",
                    "changes": [],
                    "metric_before": 0.5,
                    "metric_after": 0.7,
                    "verdict": "keep",
                    "learnings": "tweak helped",
                    "cost_usd": 0.01,
                },
                CTX,
            )

        # No regression → verdict should remain keep
        new_state = json.loads(store[f"experiment:{exp_id}"])
        last_iter = new_state["iterations"][-1]
        assert last_iter["verdict"] == "keep"
