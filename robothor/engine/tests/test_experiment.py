"""Tests for AutoResearch experiment tools."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.experiment import (
    _calc_improvement,
    _check_experiment_guardrails,
    _parse_metric_value,
)

# Shared context for all handler calls
CTX = ToolContext(agent_id="auto-researcher", workspace="/tmp/test-workspace")


# ─── Unit tests for helpers ──────────────────────────────────────────


class TestParseMetricValue:
    def test_plain_number(self):
        assert _parse_metric_value("42.5") == 42.5

    def test_integer(self):
        assert _parse_metric_value("100") == 100.0

    def test_with_whitespace(self):
        assert _parse_metric_value("  3.14  \n") == 3.14

    def test_last_line(self):
        assert _parse_metric_value("Loading...\nDone\n87.3") == 87.3

    def test_negative(self):
        assert _parse_metric_value("-0.5") == -0.5

    def test_unparseable(self):
        with pytest.raises(ValueError, match="Could not parse"):
            _parse_metric_value("no numbers here")

    def test_empty(self):
        with pytest.raises(ValueError, match="Could not parse"):
            _parse_metric_value("")


class TestCalcImprovement:
    def test_maximize_improvement(self):
        # 100 -> 120 = +20%
        assert _calc_improvement(100, 120, "maximize") == pytest.approx(20.0)

    def test_maximize_degradation(self):
        # 100 -> 80 = -20%
        assert _calc_improvement(100, 80, "maximize") == pytest.approx(-20.0)

    def test_minimize_improvement(self):
        # 100 -> 80 = +20% (lower is better)
        assert _calc_improvement(100, 80, "minimize") == pytest.approx(20.0)

    def test_minimize_degradation(self):
        # 100 -> 120 = -20% (higher is worse)
        assert _calc_improvement(100, 120, "minimize") == pytest.approx(-20.0)

    def test_zero_baseline(self):
        assert _calc_improvement(0, 50, "maximize") == 0.0

    def test_no_change(self):
        assert _calc_improvement(42, 42, "maximize") == pytest.approx(0.0)


# ─── Handler tests ───────────────────────────────────────────────────


def _mock_blocks():
    """Return a pair of patchers for read_block and write_block."""
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-03-31T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


class TestExperimentCreate:
    @pytest.mark.asyncio
    async def test_create_inline(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "test-exp",
                    "metric_name": "test metric",
                    "metric_command": "echo 42",
                    "direction": "maximize",
                    "search_space": "test files",
                },
                CTX,
            )
        assert result["success"] is True
        assert result["experiment_id"] == "test-exp"
        assert result["direction"] == "maximize"

    @pytest.mark.asyncio
    async def test_create_missing_id(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        result = await _experiment_create({}, CTX)
        assert "error" in result
        assert "experiment_id" in result["error"]

    @pytest.mark.asyncio
    async def test_create_duplicate(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        store, read_fn, write_fn = _mock_blocks()
        store["experiment:dup"] = json.dumps({"id": "dup", "status": "active"})
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create({"experiment_id": "dup"}, CTX)
        assert "error" in result
        assert "already exists" in result["error"]

    @pytest.mark.asyncio
    async def test_create_invalid_direction(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "bad-dir",
                    "metric_command": "echo 1",
                    "direction": "sideways",
                },
                CTX,
            )
        assert "error" in result
        assert "direction" in result["error"]

    @pytest.mark.asyncio
    async def test_create_caps_max_iterations(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "big-iter",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "max_iterations": 9999,
                },
                CTX,
            )
        assert result["success"] is True
        assert result["max_iterations"] == 200  # hard cap


class TestExperimentMeasure:
    @pytest.mark.asyncio
    async def test_measure_success(self):
        from robothor.engine.tools.handlers.experiment import _experiment_measure

        state = {
            "id": "m1",
            "status": "active",
            "direction": "maximize",
            "baseline_value": None,
            "current_best_value": None,
            "config": {"metric_command": "echo 42.5", "measurement_samples": 1},
        }
        store = {"experiment:m1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch(
                "robothor.engine.tools.handlers.experiment._run_metric_command",
                return_value="42.5",
            ),
        ):
            result = await _experiment_measure({"experiment_id": "m1"}, CTX)

        assert result["value"] == 42.5
        assert result["baseline_set"] is True
        assert result["num_samples"] == 1

    @pytest.mark.asyncio
    async def test_measure_not_found(self):
        from robothor.engine.tools.handlers.experiment import _experiment_measure

        with (
            patch(
                "robothor.memory.blocks.read_block",
                return_value={"error": "not found"},
            ),
        ):
            result = await _experiment_measure({"experiment_id": "nope"}, CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_measure_multiple_samples(self):
        from robothor.engine.tools.handlers.experiment import _experiment_measure

        state = {
            "id": "m2",
            "status": "active",
            "direction": "maximize",
            "baseline_value": 10.0,
            "current_best_value": 10.0,
            "config": {"metric_command": "echo 20", "measurement_samples": 1},
        }
        store = {"experiment:m2": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        call_count = 0

        def mock_cmd(cmd, ws):
            nonlocal call_count
            call_count += 1
            return str(18 + call_count)  # 19, 20, 21

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch(
                "robothor.engine.tools.handlers.experiment._run_metric_command",
                side_effect=mock_cmd,
            ),
        ):
            result = await _experiment_measure({"experiment_id": "m2", "samples": 3}, CTX)

        assert result["num_samples"] == 3
        assert result["value"] == 20.0  # mean of 19, 20, 21


class TestExperimentCommit:
    def _make_state(self, **overrides):
        base = {
            "id": "c1",
            "metric_name": "test",
            "direction": "maximize",
            "status": "active",
            "created_at": "2026-03-31T00:00:00",
            "baseline_value": 50.0,
            "current_best_value": 50.0,
            "current_best_iteration": None,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 0,
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "config": {
                "max_iterations": 20,
                "cost_budget_usd": 5.0,
                "revert_command": "",
                "notify_on_improvement_pct": 10.0,
            },
            "iterations": [],
            "learnings": {"positive": [], "negative": []},
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_commit_keep(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state()
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "c1",
                    "hypothesis": "Shorter instructions improve accuracy",
                    "changes": [{"file": "brain/agents/X.md", "description": "Simplified rules"}],
                    "metric_before": 50.0,
                    "metric_after": 55.0,
                    "verdict": "keep",
                    "learnings": "Simpler rules helped because agents spent less time parsing",
                },
                CTX,
            )

        assert result["success"] is True
        assert result["verdict"] == "keep"
        assert result["improvement_pct"] == 10.0
        assert result["total_iterations"] == 1

        # Verify state was updated
        saved = json.loads(store["experiment:c1"])
        assert saved["current_best_value"] == 55.0
        assert len(saved["learnings"]["positive"]) == 1

    @pytest.mark.asyncio
    async def test_commit_revert_runs_command(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state()
        state["config"]["revert_command"] = "git checkout -- test.md"
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        mock_run = MagicMock()
        mock_run.return_value = MagicMock(stdout="reverted", stderr="", returncode=0)

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch("robothor.engine.tools.handlers.experiment.subprocess.run", mock_run),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "c1",
                    "hypothesis": "Test revert",
                    "changes": [{"file": "test.md", "description": "bad change"}],
                    "metric_before": 50.0,
                    "metric_after": 48.0,
                    "verdict": "revert",
                    "learnings": "Change was harmful",
                },
                CTX,
            )

        assert result["verdict"] == "revert"
        assert "revert_output" in result
        mock_run.assert_called_once()

        saved = json.loads(store["experiment:c1"])
        assert len(saved["learnings"]["negative"]) == 1

    @pytest.mark.asyncio
    async def test_commit_max_iterations_terminates(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state(total_iterations=19)
        state["config"]["max_iterations"] = 20
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "c1",
                    "hypothesis": "Final iteration",
                    "changes": [{"file": "x.md", "description": "last change"}],
                    "metric_before": 50.0,
                    "metric_after": 52.0,
                    "verdict": "keep",
                    "learnings": "Good result",
                },
                CTX,
            )

        assert result["status"] == "completed"
        assert result["termination_reason"] == "max_iterations_reached"

    @pytest.mark.asyncio
    async def test_commit_cost_budget_terminates(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state(total_cost_usd=4.5)
        state["config"]["cost_budget_usd"] = 5.0
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "c1",
                    "hypothesis": "Expensive iteration",
                    "changes": [{"file": "x.md", "description": "change"}],
                    "metric_before": 50.0,
                    "metric_after": 51.0,
                    "verdict": "keep",
                    "learnings": "OK",
                    "cost_usd": 0.6,
                },
                CTX,
            )

        assert result["status"] == "completed"
        assert result["termination_reason"] == "cost_budget_exhausted"

    @pytest.mark.asyncio
    async def test_commit_consecutive_no_improvement_warning(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state(consecutive_no_improvement=2)
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "c1",
                    "hypothesis": "Another attempt",
                    "changes": [{"file": "x.md", "description": "tweak"}],
                    "metric_before": 50.0,
                    "metric_after": 49.0,
                    "verdict": "revert",
                    "learnings": "Still not working",
                },
                CTX,
            )

        assert "warning" in result
        assert "3+" in result["warning"]

    @pytest.mark.asyncio
    async def test_commit_missing_fields(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state()
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        ):
            result = await _experiment_commit(
                {"experiment_id": "c1", "hypothesis": "test"},
                CTX,
            )
        assert "error" in result
        assert "Missing required" in result["error"]

    @pytest.mark.asyncio
    async def test_commit_announce_on_threshold(self):
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = self._make_state()
        state["config"]["notify_on_improvement_pct"] = 5.0
        store = {"experiment:c1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "c1",
                    "hypothesis": "Big win",
                    "changes": [{"file": "x.md", "description": "major change"}],
                    "metric_before": 50.0,
                    "metric_after": 58.0,
                    "verdict": "keep",
                    "learnings": "Huge improvement from simplification",
                },
                CTX,
            )

        assert result["announce"] is True
        assert "announcement" in result


class TestExperimentStatus:
    @pytest.mark.asyncio
    async def test_status_compact(self):
        from robothor.engine.tools.handlers.experiment import _experiment_status

        state = {
            "id": "s1",
            "metric_name": "test",
            "direction": "maximize",
            "status": "active",
            "baseline_value": 50.0,
            "current_best_value": 55.0,
            "current_best_iteration": 2,
            "cumulative_improvement_pct": 10.0,
            "total_iterations": 3,
            "total_cost_usd": 0.45,
            "consecutive_no_improvement": 0,
            "created_at": "2026-03-31T00:00:00",
            "updated_at": "2026-03-31T01:00:00",
            "config": {"search_space": "test files", "max_iterations": 20, "cost_budget_usd": 5.0},
            "learnings": {"positive": ["good stuff"], "negative": ["bad stuff"]},
            "iterations": [{"number": 1}, {"number": 2}, {"number": 3}],
        }
        store = {"experiment:s1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        ):
            result = await _experiment_status({"experiment_id": "s1"}, CTX)

        assert result["experiment_id"] == "s1"
        assert result["cumulative_improvement_pct"] == 10.0
        assert "iterations" not in result  # compact by default

    @pytest.mark.asyncio
    async def test_status_with_iterations(self):
        from robothor.engine.tools.handlers.experiment import _experiment_status

        state = {
            "id": "s2",
            "metric_name": "test",
            "direction": "maximize",
            "status": "active",
            "baseline_value": 50.0,
            "current_best_value": 50.0,
            "current_best_iteration": None,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 1,
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "created_at": "2026-03-31T00:00:00",
            "config": {"search_space": "", "max_iterations": 20, "cost_budget_usd": 5.0},
            "learnings": {"positive": [], "negative": []},
            "iterations": [{"number": 1, "verdict": "keep"}],
        }
        store = {"experiment:s2": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        ):
            result = await _experiment_status(
                {"experiment_id": "s2", "include_iterations": True}, CTX
            )

        assert "iterations" in result
        assert len(result["iterations"]) == 1

    @pytest.mark.asyncio
    async def test_status_not_found(self):
        from robothor.engine.tools.handlers.experiment import _experiment_status

        with (
            patch(
                "robothor.memory.blocks.read_block",
                return_value={"error": "not found"},
            ),
        ):
            result = await _experiment_status({"experiment_id": "nope"}, CTX)
        assert "error" in result


# ─── Constants registration ──────────────────────────────────────────


class TestExperimentConstants:
    def test_experiment_tools_frozenset(self):
        from robothor.engine.tools.constants import EXPERIMENT_TOOLS

        assert "experiment_create" in EXPERIMENT_TOOLS
        assert "experiment_measure" in EXPERIMENT_TOOLS
        assert "experiment_commit" in EXPERIMENT_TOOLS
        assert "experiment_status" in EXPERIMENT_TOOLS

    def test_experiment_status_in_readonly(self):
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert "experiment_status" in READONLY_TOOLS

    def test_experiment_create_not_readonly(self):
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert "experiment_create" not in READONLY_TOOLS
        assert "experiment_measure" not in READONLY_TOOLS
        assert "experiment_commit" not in READONLY_TOOLS


# ─── Guardrail enforcement tests ────────────────────────────────────


class TestExperimentGuardrails:
    def test_no_guardrails_allows_all(self):
        config: dict[str, Any] = {"guardrails": []}
        changes = [{"file": "anywhere/file.py", "description": "whatever"}]
        assert _check_experiment_guardrails(config, changes) is None

    def test_missing_guardrails_key_allows_all(self):
        config: dict[str, Any] = {}
        changes = [{"file": "anywhere/file.py", "description": "whatever"}]
        assert _check_experiment_guardrails(config, changes) is None

    def test_write_path_restrict_allows_valid_path(self):
        config = {
            "guardrails": ["write_path_restrict"],
            "write_path_allowlist": ["brain/agents/*.md", "docs/agents/*.yaml"],
        }
        changes = [{"file": "brain/agents/FOO.md", "description": "edit"}]
        assert _check_experiment_guardrails(config, changes) is None

    def test_write_path_restrict_blocks_disallowed_path(self):
        config = {
            "guardrails": ["write_path_restrict"],
            "write_path_allowlist": ["brain/agents/*.md"],
        }
        changes = [{"file": "robothor/engine/runner.py", "description": "edit"}]
        result = _check_experiment_guardrails(config, changes)
        assert result is not None
        assert "runner.py" in result
        assert "write_path_allowlist" in result

    def test_write_path_restrict_multiple_changes_one_bad(self):
        config = {
            "guardrails": ["write_path_restrict"],
            "write_path_allowlist": ["brain/agents/*.md"],
        }
        changes = [
            {"file": "brain/agents/OK.md", "description": "fine"},
            {"file": "robothor/engine/tools.py", "description": "not fine"},
        ]
        result = _check_experiment_guardrails(config, changes)
        assert result is not None
        assert "tools.py" in result

    def test_exec_allowlist_allows_matching_revert(self):
        config = {
            "guardrails": ["exec_allowlist"],
            "exec_allowlist": [r"^git checkout -- ", r"^git diff"],
        }
        result = _check_experiment_guardrails(
            config, [], revert_command="git checkout -- brain/agents/X.md"
        )
        assert result is None

    def test_exec_allowlist_blocks_disallowed_revert(self):
        config = {
            "guardrails": ["exec_allowlist"],
            "exec_allowlist": [r"^git checkout -- "],
        }
        result = _check_experiment_guardrails(config, [], revert_command="rm -rf /tmp/data")
        assert result is not None
        assert "revert_command" in result

    def test_no_revert_command_skips_exec_check(self):
        config = {
            "guardrails": ["exec_allowlist"],
            "exec_allowlist": [r"^git checkout -- "],
        }
        result = _check_experiment_guardrails(config, [], revert_command=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_commit_with_guardrail_violation_returns_error(self):
        """Integration test: experiment_commit rejects disallowed file paths."""
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = {
            "id": "g1",
            "metric_name": "test",
            "direction": "maximize",
            "status": "active",
            "created_at": "2026-04-09T00:00:00",
            "baseline_value": 50.0,
            "current_best_value": 50.0,
            "current_best_iteration": None,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 0,
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "config": {
                "max_iterations": 20,
                "cost_budget_usd": 5.0,
                "revert_command": "",
                "notify_on_improvement_pct": 10.0,
                "guardrails": ["write_path_restrict"],
                "write_path_allowlist": ["brain/agents/*.md"],
            },
            "iterations": [],
            "learnings": {"positive": [], "negative": []},
        }
        store = {"experiment:g1": json.dumps(state)}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "g1",
                    "hypothesis": "test",
                    "changes": [{"file": "robothor/engine/runner.py", "description": "bad"}],
                    "metric_before": 50.0,
                    "metric_after": 55.0,
                    "verdict": "keep",
                    "learnings": "test",
                },
                CTX,
            )

        assert "error" in result
        assert result.get("guardrail_violation") is True


# ─── Advisory file locking tests ────────────────────────────────────


class TestExperimentFileLocking:
    @pytest.mark.asyncio
    async def test_create_acquires_locks(self):
        """experiment_create writes advisory locks for search-space files."""
        from robothor.engine.tools.handlers.experiment import _experiment_create

        store: dict[str, str] = {}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "lock-test",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "search_space": "brain/agents/X.md, docs/agents/x.yaml",
                },
                CTX,
            )

        assert result["success"] is True
        assert store.get("experiment_lock:brain/agents/X.md") == "lock-test"
        assert store.get("experiment_lock:docs/agents/x.yaml") == "lock-test"

    @pytest.mark.asyncio
    async def test_create_fails_if_locked_by_other(self):
        """experiment_create fails if files are locked by another experiment."""
        from robothor.engine.tools.handlers.experiment import _experiment_create

        store = {"experiment_lock:brain/agents/X.md": "other-experiment"}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "blocked",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "search_space": "brain/agents/X.md",
                },
                CTX,
            )

        assert "error" in result
        assert "other-experiment" in result["error"]

    @pytest.mark.asyncio
    async def test_create_allows_same_experiment_relock(self):
        """experiment_create allows relocking files already held by the same experiment."""
        from robothor.engine.tools.handlers.experiment import _experiment_create

        # Pre-existing lock by same experiment ID shouldn't block
        store = {"experiment_lock:brain/agents/X.md": "relock-test"}

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "relock-test",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "search_space": "brain/agents/X.md",
                },
                CTX,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_commit_completion_releases_locks(self):
        """Locks are released when experiment terminates."""
        from robothor.engine.tools.handlers.experiment import _experiment_commit

        state = {
            "id": "rel1",
            "metric_name": "test",
            "direction": "maximize",
            "status": "active",
            "created_at": "2026-04-09T00:00:00",
            "baseline_value": 50.0,
            "current_best_value": 50.0,
            "current_best_iteration": None,
            "cumulative_improvement_pct": 0.0,
            "total_iterations": 19,  # One before max (20) → will complete
            "total_cost_usd": 0.0,
            "consecutive_no_improvement": 0,
            "config": {
                "max_iterations": 20,
                "cost_budget_usd": 50.0,
                "revert_command": "",
                "notify_on_improvement_pct": 10.0,
                "search_space": "brain/agents/X.md",
            },
            "iterations": [],
            "learnings": {"positive": [], "negative": []},
        }
        store = {
            "experiment:rel1": json.dumps(state),
            "experiment_lock:brain/agents/X.md": "rel1",
        }

        def read_fn(name):
            if name in store:
                return {"content": store[name]}
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await _experiment_commit(
                {
                    "experiment_id": "rel1",
                    "hypothesis": "final iteration",
                    "changes": [{"file": "brain/agents/X.md", "description": "edit"}],
                    "metric_before": 50.0,
                    "metric_after": 55.0,
                    "verdict": "keep",
                    "learnings": "done",
                },
                CTX,
            )

        assert result["success"] is True
        assert result["status"] == "completed"
        # Lock should be cleared (empty string)
        assert store.get("experiment_lock:brain/agents/X.md") == ""


# ─── search_space parser + staleness tests ──────────────────────────


class TestSearchSpaceParser:
    """_parse_search_space only emits clean file paths; malformed inputs are discarded."""

    def test_simple_comma_list(self):
        from robothor.engine.tools.handlers.experiment import _parse_search_space

        assert _parse_search_space("a/b.md, c/d.yaml") == ["a/b.md", "c/d.yaml"]

    def test_strips_parenthetical_descriptions(self):
        from robothor.engine.tools.handlers.experiment import _parse_search_space

        # "brain/X.md (instruction file — simplify)" → "brain/X.md"
        result = _parse_search_space(
            "brain/EMAIL_CLASSIFIER.md (instruction file), docs/agents/email-classifier.yaml (manifest: tools)"
        )
        assert result == ["brain/EMAIL_CLASSIFIER.md", "docs/agents/email-classifier.yaml"]

    def test_rejects_multiline_prose(self):
        from robothor.engine.tools.handlers.experiment import _parse_search_space

        # Agent writes a multi-line instruction as search_space; parser must not treat it as paths
        result = _parse_search_space(
            "You may modify:\n- brain/EMAIL_CLASSIFIER.md (instruction file — simplify branching\n- reduce complexity)"
        )
        assert result == ["brain/EMAIL_CLASSIFIER.md"]

    def test_rejects_standalone_words(self):
        from robothor.engine.tools.handlers.experiment import _parse_search_space

        # "model" and "timeouts)" should not become lock keys
        result = _parse_search_space("brain/X.md, model, timeouts)")
        assert result == ["brain/X.md"]

    def test_empty_and_whitespace(self):
        from robothor.engine.tools.handlers.experiment import _parse_search_space

        assert _parse_search_space("") == []
        assert _parse_search_space("   ,  , ") == []

    def test_dedups(self):
        from robothor.engine.tools.handlers.experiment import _parse_search_space

        assert _parse_search_space("a/b.md, a/b.md, c.yaml") == ["a/b.md", "c.yaml"]


class TestLockStaleness:
    """Locks held longer than LOCK_STALENESS_HOURS are reclaimable."""

    @pytest.mark.asyncio
    async def test_stale_lock_is_reclaimed(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        store = {"experiment_lock:brain/agents/X.md": "abandoned-exp"}
        written_at: dict[str, str] = {
            # 48 hours stale
            "experiment_lock:brain/agents/X.md": "2026-04-14T12:00:00-04:00"
        }

        def read_fn(name):
            if name in store:
                return {
                    "content": store[name],
                    "last_written_at": written_at.get(name, "2026-04-16T12:00:00-04:00"),
                }
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            written_at[name] = "2026-04-16T12:00:00-04:00"
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch(
                "robothor.engine.tools.handlers.experiment._now_iso",
                return_value="2026-04-16T12:00:00-04:00",
            ),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "new-owner",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "search_space": "brain/agents/X.md",
                },
                CTX,
            )

        assert result["success"] is True
        # New experiment should have reclaimed the stale lock
        assert store["experiment_lock:brain/agents/X.md"] == "new-owner"

    @pytest.mark.asyncio
    async def test_fresh_lock_still_blocks(self):
        from robothor.engine.tools.handlers.experiment import _experiment_create

        store = {"experiment_lock:brain/agents/X.md": "active-exp"}
        written_at = {
            # Only 1h old
            "experiment_lock:brain/agents/X.md": "2026-04-16T11:00:00-04:00"
        }

        def read_fn(name):
            if name in store:
                return {
                    "content": store[name],
                    "last_written_at": written_at.get(name),
                }
            return {"error": "not found"}

        def write_fn(name, content):
            store[name] = content
            return {"success": True}

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch(
                "robothor.engine.tools.handlers.experiment._now_iso",
                return_value="2026-04-16T12:00:00-04:00",
            ),
        ):
            result = await _experiment_create(
                {
                    "experiment_id": "new-contender",
                    "metric_command": "echo 1",
                    "direction": "maximize",
                    "search_space": "brain/agents/X.md",
                },
                CTX,
            )

        assert "error" in result
        assert "active-exp" in result["error"]
