"""Sandbox-by-default policy for exec-holding agents (Wave-1 hardening, PR-10).

The only live compute path was exec -> subprocess on the host. This defaults
exec-holding agents into the per-run Docker sandbox under the
sandbox_default_mode ladder, with an explicit `sandbox: host` opt-out and a
graceful host fallback (handled by the runner try/except).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import (
    AgentRunner,
    _agent_holds_exec,
    _resolve_sandbox_decision,
)


def _cfg(sample_agent_config, **over):
    return dataclasses.replace(sample_agent_config, **over)


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = []
        mock_registry.get_tool_names.return_value = []
        mock_reg.return_value = mock_registry
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


class TestAgentHoldsExec:
    def test_exec_in_allowed(self, sample_agent_config):
        assert _agent_holds_exec(_cfg(sample_agent_config, tools_allowed=["exec", "read_file"]))

    def test_empty_allowed_is_full_set(self, sample_agent_config):
        assert _agent_holds_exec(_cfg(sample_agent_config, tools_allowed=[]))

    def test_exec_absent(self, sample_agent_config):
        assert not _agent_holds_exec(_cfg(sample_agent_config, tools_allowed=["read_file"]))

    def test_exec_denied_overrides(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, tools_allowed=[], tools_denied=["exec"])
        assert not _agent_holds_exec(cfg)


class TestSandboxDecision:
    def test_explicit_docker_always_sandboxes(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, sandbox="docker", tools_allowed=["read_file"])
        assert _resolve_sandbox_decision(cfg, "off") == "docker"

    def test_explicit_host_always_opts_out(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, sandbox="host", tools_allowed=["exec"])
        assert _resolve_sandbox_decision(cfg, "enforce") == "host"

    def test_off_runs_on_host(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, sandbox="local", tools_allowed=["exec"])
        assert _resolve_sandbox_decision(cfg, "off") == "host"

    def test_observe_logs_but_runs_host(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, sandbox="local", tools_allowed=["exec"])
        assert _resolve_sandbox_decision(cfg, "observe") == "observe"

    def test_enforce_sandboxes_exec_agent(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, sandbox="local", tools_allowed=["exec"])
        assert _resolve_sandbox_decision(cfg, "enforce") == "docker"

    def test_enforce_skips_non_exec_agent(self, sample_agent_config):
        cfg = _cfg(sample_agent_config, sandbox="local", tools_allowed=["read_file"])
        assert _resolve_sandbox_decision(cfg, "enforce") == "host"


class TestSandboxObserveEmission:
    @pytest.mark.asyncio
    async def test_observe_mode_emits_shadow_guardrail_event(
        self, runner, sample_agent_config, mock_litellm_response, monkeypatch
    ):
        """In observe mode an exec-holding agent runs on host but emits an
        'observed' sandbox_default guardrail event so the operator sees impact."""
        monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)
        monkeypatch.setenv("ROBOTHOR_SANDBOX_DEFAULT_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_SANDBOX_DEFAULT_MODE", "observe")

        cfg = _cfg(sample_agent_config, sandbox="local", tools_allowed=["exec"])
        events = []

        def _capture(**kwargs):
            events.append(kwargs)

        response = mock_litellm_response(content="done")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("robothor.engine.tracking.log_guardrail_event", _capture),
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=response),
        ):
            run = await runner.execute(
                "test-agent", "go", trigger_type=TriggerType.CRON, agent_config=cfg
            )

        assert run.status == RunStatus.COMPLETED  # observe never blocks
        sandbox_events = [
            e
            for e in events
            if e.get("guardrail_name") == "sandbox_default" and e.get("action") == "observed"
        ]
        assert sandbox_events, f"no observed sandbox_default event emitted; got {events}"
