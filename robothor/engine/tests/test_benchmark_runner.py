"""Tests for benchmark_run_for_agent and benchmark_run_fleet — the daily
fleet grader entry points added 2026-05-06.

These tests verify the wiring without spinning up real sub-agents:
- auto_define_suite_from_disk loads + validates a suite.yaml file
- benchmark_run_for_agent loads → runs → writes to benchmark_results
- benchmark_run_fleet iterates docs/benchmarks/*/suite.yaml
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _mock_blocks():
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-05-06T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _make_mock_run(output_text: str = "calendar event found", cost: float = 0.05):
    run = MagicMock()
    run.output_text = output_text
    run.total_cost_usd = cost
    run.steps = [MagicMock(), MagicMock()]
    run.status = MagicMock()
    run.status.value = "completed"
    return run


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Ephemeral workspace with a single benchmark suite for `main`."""
    bench = tmp_path / "docs" / "benchmarks" / "main"
    bench.mkdir(parents=True)
    (bench / "suite.yaml").write_text(
        """
id: main-test-harness
agent_id: main
description: "Tiny suite for testing"
max_cost_usd: 0.50
tasks:
  - id: hello
    prompt: "Say hello and mention the calendar"
    category: correctness
    weight: 1.0
    expected:
      must_contain: ["calendar"]
""".strip()
    )
    return tmp_path


# ─── auto_define_suite_from_disk ─────────────────────────────────────


class TestAutoDefineSuiteFromDisk:
    @pytest.mark.asyncio
    async def test_loads_yaml_and_writes_block(self, workspace: Path):
        from robothor.engine.tools.handlers.benchmark import (
            auto_define_suite_from_disk,
        )

        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await auto_define_suite_from_disk("main", str(workspace))

        assert result["agent_id"] == "main"
        assert result["id"] == "main-test-harness"
        assert len(result["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_missing_suite_returns_error(self, tmp_path: Path):
        from robothor.engine.tools.handlers.benchmark import (
            auto_define_suite_from_disk,
        )

        result = await auto_define_suite_from_disk("nonexistent", str(tmp_path))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_suite_id_field_is_honored(self, tmp_path: Path):
        """Phase 4b: 8 fleet suites declare `suite_id:` not `id:` — must not fall
        through to <agent>-default, which scattered their benchmark_results."""
        from robothor.engine.tools.handlers.benchmark import auto_define_suite_from_disk

        bench = tmp_path / "docs" / "benchmarks" / "archy"
        bench.mkdir(parents=True)
        (bench / "suite.yaml").write_text(
            "suite_id: archy-v1\nagent_id: archy\ntasks:\n"
            "  - id: t\n    prompt: hi\n    category: correctness\n"
            "    expected: {must_contain: ['hi']}\n"
        )
        _, read_fn, write_fn = _mock_blocks()
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        ):
            result = await auto_define_suite_from_disk("archy", str(tmp_path))
        assert result["id"] == "archy-v1"


# ─── benchmark_run_for_agent ─────────────────────────────────────────


class TestBenchmarkRunForAgent:
    @pytest.mark.asyncio
    async def test_loads_suite_then_runs_writes_table(self, workspace: Path):
        from robothor.engine.tools.handlers.benchmark import (
            _benchmark_run_for_agent,
        )

        ctx = ToolContext(agent_id="benchmark-runner", workspace=str(workspace))
        _, read_fn, write_fn = _mock_blocks()

        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=_make_mock_run())
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = str(workspace / "docs" / "agents")

        mock_agent_config = MagicMock()
        mock_agent_config.max_iterations = 10
        mock_agent_config.cost_budget_usd = 1.0

        captured_inserts: list[tuple] = []

        class FakeCursor:
            def execute(self, sql, params):
                if "INSERT INTO benchmark_results" in sql:
                    captured_inserts.append(params)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch(
                "robothor.engine.tools.handlers.spawn.get_runner",
                return_value=mock_runner,
            ),
            patch(
                "robothor.engine.config.load_agent_config",
                return_value=mock_agent_config,
            ),
            patch("robothor.db.connection.get_connection", return_value=FakeConn()),
        ):
            result = await _benchmark_run_for_agent(
                {"agent_id": "main", "tag": "manual-1", "triggered_by": "manual"},
                ctx,
            )

        assert result["success"] is True
        assert result["aggregate_score"] > 0
        assert result["tasks_run"] == 1
        assert len(captured_inserts) == 1, "should have written one benchmark_results row"
        params = captured_inserts[0]
        # params order: agent_id, suite_id, suite_path, total_cases, passed,
        #               failed, pass_rate, category_scores_json, failures_json,
        #               triggered_by, experiment_id, cost_usd
        assert params[0] == "main"
        assert params[3] == 1  # total_cases
        assert params[4] == 1  # passed
        assert params[5] == 0  # failed
        assert params[9] == "manual"  # triggered_by


# ─── benchmark_run_fleet ─────────────────────────────────────────────


class TestBenchmarkRunFleet:
    @pytest.mark.asyncio
    async def test_skips_dirs_without_suite(self, tmp_path: Path):
        from robothor.engine.tools.handlers.benchmark import _benchmark_run_fleet

        # main has a suite, empty-dir does not
        bench = tmp_path / "docs" / "benchmarks"
        (bench / "main").mkdir(parents=True)
        (bench / "main" / "suite.yaml").write_text(
            """
id: main-test
agent_id: main
tasks:
  - id: t
    prompt: hi
    category: correctness
    expected: {must_contain: ["hi"]}
""".strip()
        )
        (bench / "empty-dir").mkdir()  # no suite.yaml

        # Phase 0f: the fleet runner skips suites without a live manifest, so
        # give main one (the dead-suite guard is covered separately).
        agents_dir = tmp_path / "docs" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "main.yaml").write_text("id: main\n")

        ctx = ToolContext(agent_id="benchmark-runner", workspace=str(tmp_path))
        _, read_fn, write_fn = _mock_blocks()

        # Stub out the actual run so we just verify dispatch.
        async def fake_run_for_agent(args, ctx_):
            return {
                "success": True,
                "aggregate_score": 1.0,
                "tasks_run": 1,
                "total_cost_usd": 0.01,
            }

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch(
                "robothor.engine.tools.handlers.benchmark._benchmark_run_for_agent",
                side_effect=fake_run_for_agent,
            ),
        ):
            result = await _benchmark_run_fleet({}, ctx)

        assert result["success"] is True
        assert result["agents_attempted"] == 1
        assert result["results"][0]["agent_id"] == "main"
