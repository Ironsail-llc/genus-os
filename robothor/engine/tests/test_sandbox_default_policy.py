"""Sandbox-by-default policy for exec-holding agents (Wave-1 hardening, PR-10).

The only live compute path was exec -> subprocess on the host. This defaults
exec-holding agents into the per-run Docker sandbox under the
sandbox_default_mode ladder, with an explicit `sandbox: host` opt-out and a
graceful host fallback (handled by the runner try/except).
"""

from __future__ import annotations

import dataclasses

from robothor.engine.runner import _agent_holds_exec, _resolve_sandbox_decision


def _cfg(sample_agent_config, **over):
    return dataclasses.replace(sample_agent_config, **over)


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
