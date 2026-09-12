"""Under `enforce`, a refused manifest must never surface as a traceback.

`load_agent_config` raises `ManifestSchemaError`. Every reachable call site
already has a tested answer for "this agent has no config" — an error dict, a
log line and return, a failed workflow step, a non-zero exit. A refused manifest
is a broken agent, not an unhandled exception: it has to arrive at those same
answers, and say so on the way, so an operator can tell it apart from a
deletion.

Only `ManifestSchemaError` is caught anywhere below. A guard that swallowed
`Exception` would turn the next unrelated bug into the same silent "agent has no
config", which is the failure mode this whole branch exists to remove.

The scheduler case is the sharp one. An exception out of an APScheduler job is
swallowed into an APScheduler log record, so the agent would simply stop running
with nothing an operator recognises as a cause.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from robothor.engine.config import EngineConfig

BROKEN = """\
id: bob
name: Bob
description: A generic fixture agent
version: "2026-09-11"
department: opperations
schedule:
  cron: "0 7 * * *"
"""


@pytest.fixture
def broken_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
    (tmp_path / "bob.yaml").write_text(BROKEN)
    return tmp_path


def test_the_fixture_really_does_raise(broken_fleet):
    """Every test below asserts an absence. If the manifest stopped being
    refused they would all pass while proving nothing."""
    from robothor.engine.config import load_agent_config
    from robothor.engine.manifest_schema import ManifestSchemaError

    with pytest.raises(ManifestSchemaError):
        load_agent_config("bob", broken_fleet)


class TestScheduler:
    def _scheduler(self, manifest_dir):
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=manifest_dir, workspace=manifest_dir.parent)
        return CronScheduler(config, MagicMock())

    @pytest.mark.asyncio
    async def test_a_cron_job_logs_and_returns(self, broken_fleet, caplog):
        scheduler = self._scheduler(broken_fleet)
        with caplog.at_level(logging.ERROR, logger="robothor.engine.scheduler"):
            await scheduler._run_agent("bob")
        assert any("SchemaError" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    @pytest.mark.asyncio
    async def test_a_heartbeat_job_logs_and_returns(self, broken_fleet, caplog):
        scheduler = self._scheduler(broken_fleet)
        with caplog.at_level(logging.ERROR, logger="robothor.engine.scheduler"):
            await scheduler._run_heartbeat("bob")
        assert any("SchemaError" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_worker_job_logs_and_returns(self, broken_fleet, caplog):
        scheduler = self._scheduler(broken_fleet)
        with caplog.at_level(logging.ERROR, logger="robothor.engine.scheduler"):
            await scheduler._run_worker("bob")
        assert any("SchemaError" in r.getMessage() for r in caplog.records)


class TestRunner:
    @pytest.mark.asyncio
    async def test_execute_records_a_failed_run_instead_of_raising(self, broken_fleet, caplog):
        """Every run funnels through `AgentRunner.execute`.

        An escaping raise here would land wherever execute was awaited from —
        a scheduler job, a spawn, a workflow step — each with its own idea of
        what to do with it. The run is recorded as FAILED and the SchemaError
        goes to the log; `execute` is at its exact size ratchet, so the reason
        rides in the log line rather than in four more lines of handler.
        """
        from robothor.engine.models import RunStatus, TriggerType
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent))
        with caplog.at_level(logging.ERROR):
            run = await runner.execute(
                agent_id="bob", message="hi", trigger_type=TriggerType.MANUAL
            )

        assert run.status == RunStatus.FAILED
        assert any("SchemaError" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_recorded_reason_says_rejected_by_schema(self, broken_fleet):
        """The run row is what someone reads a week later.

        "Agent config not found" sends them looking for a missing file; the
        file is there, with a typo in it. The two causes need different fixes,
        so the recorded reason has to tell them apart — not just the log line.
        """
        from robothor.engine.models import TriggerType
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent))
        run = await runner.execute(agent_id="bob", message="hi", trigger_type=TriggerType.MANUAL)

        assert "manifest rejected by schema" in (run.error_message or "").lower()
        assert "bob" in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_a_genuinely_absent_agent_still_says_not_found(self, broken_fleet):
        from robothor.engine.models import TriggerType
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent))
        run = await runner.execute(agent_id="nobody", message="hi", trigger_type=TriggerType.MANUAL)

        assert "not found" in (run.error_message or "")


class TestTelegram:
    def test_the_manifest_primary_falls_back_to_empty(self, tmp_path, monkeypatch):
        """`_get_manifest_primary` already answers "" when main has no config;
        a refused main must not crash the `/model` command instead."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        (tmp_path / "main.yaml").write_text(BROKEN.replace("bob", "main").replace("Bob", "Main"))
        from robothor.engine.telegram import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.config = EngineConfig(manifest_dir=tmp_path, workspace=tmp_path.parent)
        assert bot._get_manifest_primary() == ""

    def test_the_background_config_raises_the_runtime_error_callers_expect(
        self, tmp_path, monkeypatch
    ):
        """The existing contract for "no config" here is RuntimeError, which
        the caller already renders as a user message. A ManifestSchemaError
        escaping instead would reach the user as a traceback."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        (tmp_path / "main.yaml").write_text(BROKEN.replace("bob", "main").replace("Bob", "Main"))
        from robothor.engine.telegram import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.config = EngineConfig(
            manifest_dir=tmp_path, workspace=tmp_path.parent, default_chat_agent="main"
        )
        with pytest.raises(RuntimeError) as excinfo:
            bot._build_background_config()
        assert "SchemaError" in str(excinfo.value) or "schema" in str(excinfo.value).lower()


class TestWorkflowStep:
    @pytest.mark.asyncio
    async def test_a_step_fails_with_the_schema_reason(self, broken_fleet):
        """A workflow step already records `error_message` when the agent has
        no config, and that string is what the run report shows. A refused
        manifest must land there naming its own cause."""
        from robothor.engine.models import (
            WorkflowStepDef,
            WorkflowStepResult,
            WorkflowStepStatus,
        )
        from robothor.engine.workflow import WorkflowEngine

        engine = WorkflowEngine(
            EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent), MagicMock()
        )
        step = WorkflowStepDef(id="s1", agent_id="bob", message="go")
        result = WorkflowStepResult(step_id="s1")
        await engine._run_agent_step(step, MagicMock(), result)

        assert result.status == WorkflowStepStatus.FAILED
        assert "rejected by schema" in (result.error_message or "")
        assert "bob" in (result.error_message or "")


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawning_a_broken_child_returns_an_error_dict(self, broken_fleet, monkeypatch):
        from robothor.engine.models import SpawnContext
        from robothor.engine.tools.handlers import spawn as spawn_mod

        runner = MagicMock()
        runner.config = EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent)
        monkeypatch.setattr(spawn_mod, "get_runner", lambda: runner)
        ctx = SpawnContext(
            parent_run_id="r1",
            parent_agent_id="alice",
            correlation_id="c1",
            nesting_depth=0,
            max_nesting_depth=2,
        )
        token = spawn_mod._current_spawn_context.set(ctx)
        try:
            out = await spawn_mod._handle_spawn_agent(
                {"agent_id": "bob", "message": "go"}, agent_id="alice"
            )
        finally:
            spawn_mod._current_spawn_context.reset(token)

        assert "error" in out
        assert "rejected by schema" in out["error"]


class TestBenchmark:
    @pytest.mark.asyncio
    async def test_a_refused_agent_scores_zero_with_a_reason(self, monkeypatch):
        """A benchmark suite must not abort partway through.

        The task row already carries an `error` when the agent has no config.
        A refused manifest belongs in that row — half a suite plus a traceback
        is a result nobody can compare against anything.
        """
        import json
        from unittest.mock import AsyncMock, patch

        from robothor.engine.manifest_schema import ManifestIssue, ManifestSchemaError
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.benchmark import _benchmark_run

        store = {
            "benchmark:main:test-suite": json.dumps(
                {
                    "id": "test-suite",
                    "agent_id": "main",
                    "max_cost_usd": 1.0,
                    "tasks": [
                        {
                            "id": "t1",
                            "prompt": "do a thing",
                            "category": "correctness",
                            "weight": 1.0,
                            "expected": {"must_contain": ["thing"]},
                        }
                    ],
                }
            )
        }

        def read_block(name: str) -> dict:
            if name in store:
                return {"content": store[name], "last_written_at": "2026-09-11T00:00:00"}
            return {"error": f"Block '{name}' not found"}

        def write_block(name: str, content: str) -> dict:
            store[name] = content
            return {"success": True, "block_name": name}

        runner = MagicMock()
        runner.execute = AsyncMock()
        runner.config = MagicMock()
        runner.config.manifest_dir = "/tmp"

        # The result INSERT is guarded against non-*_test databases, and the
        # guard reads the DSN off the connection — so the fake has to answer.
        conn = MagicMock()
        conn.get_dsn_parameters.return_value = {"dbname": "robothor_test"}
        get_connection = MagicMock()
        get_connection.return_value.__enter__.return_value = conn

        refusal = ManifestSchemaError(
            "main", [ManifestIssue("department", "invalid_enum", "bad", "error")]
        )
        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_block),
            patch("robothor.memory.blocks.write_block", side_effect=write_block),
            patch("robothor.db.connection.get_connection", get_connection),
            patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=runner),
            patch("robothor.engine.config.load_agent_config", side_effect=refusal),
        ):
            out = await _benchmark_run(
                {"agent_id": "main", "suite_id": "test-suite", "tag": "baseline"},
                ToolContext(agent_id="auto-agent", workspace="/tmp/test-workspace"),
            )

        rows = out.get("task_results") or []
        assert rows, out
        assert rows[0]["score"] == 0.0
        assert "rejected by schema" in rows[0]["error"]
        runner.execute.assert_not_awaited()


class TestSitesBehindABroadExcept:
    """Three sites already wrapped `load_agent_config` in `except Exception`.

    They therefore returned the right SHAPE for a refused manifest by accident,
    and said the wrong thing about it: "Could not load agent config", the same
    line a missing file or a permissions problem produces. A broad catch that
    hides a specific, named, actionable failure is the inert-control pattern —
    it looks handled and tells nobody what happened.

    So these assert on the LOG, not just the fallback value: the SchemaError
    must be named, and the generic line must not be the only thing said. The
    broad except stays for genuinely unexpected errors.
    """

    @pytest.fixture
    def ma_workspace(self, tmp_path, monkeypatch):
        """Managed agents derive the manifest dir from ROBOTHOR_WORKSPACE."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        agents = tmp_path / "docs" / "agents"
        agents.mkdir(parents=True)
        (agents / "bob.yaml").write_text(BROKEN)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        return tmp_path

    def test_tools_fall_back_to_empty_and_name_the_schema(self, ma_workspace, caplog):
        from robothor.engine.managed_agents import runner as ma_runner

        with caplog.at_level(logging.ERROR):
            tools = ma_runner._build_tools("bob", None, enable_builtin_sandbox=False)

        assert tools == []
        assert any("SchemaError" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_the_system_prompt_falls_back_to_empty_and_names_the_schema(self, ma_workspace, caplog):
        from robothor.engine.managed_agents import runner as ma_runner

        with caplog.at_level(logging.ERROR):
            assert ma_runner._load_system_prompt("bob") == ""

        assert any("SchemaError" in r.getMessage() for r in caplog.records)

    def test_the_review_model_falls_back_and_names_the_schema(
        self, broken_fleet, monkeypatch, caplog
    ):
        from robothor.engine import buddy_critic

        (broken_fleet / "buddy.yaml").write_text(BROKEN.replace("bob", "buddy"))
        monkeypatch.setattr(buddy_critic, "AGENTS_DIR", broken_fleet)
        monkeypatch.setattr(buddy_critic, "_review_model_cache", None)

        with caplog.at_level(logging.ERROR):
            assert buddy_critic._get_review_model() == buddy_critic.DEFAULT_REVIEW_MODEL

        assert any("SchemaError" in r.getMessage() for r in caplog.records)


class TestCli:
    def test_run_exits_non_zero_without_a_traceback(self, broken_fleet, monkeypatch, capsys):
        from argparse import Namespace

        from robothor.cli import engine as cli_engine

        monkeypatch.setattr(
            EngineConfig,
            "from_env",
            classmethod(
                lambda cls: EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent)
            ),
        )
        args = Namespace(
            message="hello", agent="bob", print_only=True, json_output=False, workspace=None
        )
        assert cli_engine.cmd_run(args) == 1
        err = capsys.readouterr().err
        assert "Error: Agent manifest rejected by schema: bob" in err

    def test_it_prints_exactly_one_line_and_not_the_contradicting_one(
        self, broken_fleet, monkeypatch, capsys
    ):
        """ "rejected by schema" and "not found in <dir>" are contradictory.

        The file is right there. Printing both sends the operator looking for
        something that is not missing.
        """
        from argparse import Namespace

        from robothor.cli import engine as cli_engine

        monkeypatch.setattr(
            EngineConfig,
            "from_env",
            classmethod(
                lambda cls: EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent)
            ),
        )
        args = Namespace(
            message="hello", agent="bob", print_only=True, json_output=False, workspace=None
        )
        cli_engine.cmd_run(args)
        captured = capsys.readouterr()
        lines = [ln for ln in (captured.err + captured.out).splitlines() if ln.strip()]

        assert len(lines) == 1, lines
        assert "not found" not in lines[0]

    def test_a_genuinely_absent_agent_still_says_not_found(self, broken_fleet, monkeypatch, capsys):
        from argparse import Namespace

        from robothor.cli import engine as cli_engine

        monkeypatch.setattr(
            EngineConfig,
            "from_env",
            classmethod(
                lambda cls: EngineConfig(manifest_dir=broken_fleet, workspace=broken_fleet.parent)
            ),
        )
        args = Namespace(
            message="hello", agent="nobody", print_only=True, json_output=False, workspace=None
        )
        assert cli_engine.cmd_run(args) == 1
        assert "not found" in capsys.readouterr().err
