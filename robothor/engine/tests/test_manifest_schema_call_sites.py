"""Under `enforce`, a refused manifest must never surface as a traceback.

`load_agent_config` raises `ManifestSchemaError`. Five call sites called it with
no guard at all, each with an existing, tested answer for "this agent has no
config" — an error dict, a log line and return, a non-zero exit. A refused
manifest is a broken agent, not an unhandled exception: it has to arrive at
those same answers, and say `SchemaError` on the way so an operator can tell it
apart from a deletion.

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
        assert "bob" in capsys.readouterr().err
