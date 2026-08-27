"""Tests for Agent Architect — fleet evolution meta-agent.

Validates the manifest configuration, system integration wiring
(AutoAgent, AutoResearch, scheduler), and model registry entries.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from robothor.engine.config import load_manifest, manifest_to_agent_config

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

# ─── Constants ──────────────────────────────────────────────────────

MANIFEST_PATH = Path(__file__).resolve().parents[3] / "docs" / "agents" / "agent-architect.yaml"
AUTO_AGENT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "agents" / "auto-agent.yaml"
)
AUTO_RESEARCHER_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "agents" / "auto-researcher.yaml"
)

# Agent manifests are instance data (.gitignored) — skip these tests in CI
_MANIFESTS_AVAILABLE = MANIFEST_PATH.exists()
pytestmark = pytest.mark.skipif(
    not _MANIFESTS_AVAILABLE,
    reason="Agent manifests are instance config, not present in clean checkout",
)


# ─── Helpers ────────────────────────────────────────────────────────


def _load_manifest_checked(path: Path) -> dict:
    """Load a manifest, failing the test if the file is missing."""
    manifest = load_manifest(path)
    assert manifest is not None, f"Manifest not found: {path}"
    return manifest


def _load_architect_config() -> AgentConfig:
    """Load the agent-architect manifest into an AgentConfig."""
    return manifest_to_agent_config(_load_manifest_checked(MANIFEST_PATH))


# ═══════════════════════════════════════════════════════════════════
# Manifest & Configuration
# ═══════════════════════════════════════════════════════════════════


class TestManifestConfiguration:
    """Verify the agent-architect.yaml manifest is valid and well-formed."""

    def test_manifest_loads_cleanly(self):
        """Manifest parses into a valid AgentConfig without errors.

        The model assignment is asserted against the manifest itself (its
        ``model.primary``) rather than a hardcoded literal — model migrations
        change the manifest frequently and must not break this structural
        check. (A hardcoded literal here silently went stale across the
        codex/gpt-5.5 migration; audit 2026-05-29.)
        """
        manifest = _load_manifest_checked(MANIFEST_PATH)
        config = _load_architect_config()
        assert config.id == "agent-architect"
        expected_model = manifest.get("model", {}).get("primary")
        assert expected_model, "manifest is missing model.primary"
        assert config.model_primary == expected_model

    def test_manifest_tools_registered(self):
        """Engine-native tools in tools_allowed exist in the ToolRegistry.

        Some tools (memory_block_*, list_tasks, create_task, resolve_task,
        search_memory) are provided via the Bridge MCP server, not the
        native engine registry. We only check native tools here.
        """
        mcp_tools = {
            "memory_block_read",
            "memory_block_write",
            "memory_block_list",
            "append_to_block",
            "list_tasks",
            "create_task",
            "resolve_task",
            "search_memory",
        }
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools.registry import ToolRegistry

            registry = ToolRegistry()
            config = _load_architect_config()
            for tool in config.tools_allowed:
                if tool in mcp_tools:
                    continue  # Provided by Bridge MCP, not native registry
                assert tool in registry._schemas, f"Tool '{tool}' not in native registry"

    def test_manifest_denied_tools_blocked(self):
        """Dangerous tools are in tools_denied."""
        config = _load_architect_config()
        denied = set(config.tools_denied)
        assert "exec" in denied
        assert "experiment_create" in denied
        assert "experiment_measure" in denied
        assert "experiment_commit" in denied
        assert "benchmark_define" in denied
        assert "benchmark_run" in denied

    def test_manifest_relationships_valid(self):
        """reports_to, creates_tasks_for, escalates_to reference real agents."""
        config = _load_architect_config()
        assert config.reports_to == "main"
        # creates_tasks_for and escalates_to are manifest-level, check raw manifest
        manifest = _load_manifest_checked(MANIFEST_PATH)
        assert "auto-agent" in manifest["creates_tasks_for"]
        assert "auto-researcher" in manifest["creates_tasks_for"]
        assert manifest["escalates_to"] == "main"

    def test_manifest_warmup_blocks_declared(self):
        """All required memory blocks are in warmup.memory_blocks."""
        manifest = _load_manifest_checked(MANIFEST_PATH)
        blocks = manifest["warmup"]["memory_blocks"]
        expected = {
            "architect_evolution_log",
            "architect_dispatch_ledger",
            "autoagent_learnings",
            "autoresearch_learnings",
            "performance_baselines",
            "self_model",
            "watchdog_log",
            "autodream_log",
        }
        assert expected == set(blocks)

    def test_manifest_fallback_models_registered(self):
        """All fallback models are in the model registry."""
        from robothor.engine.model_registry import get_model_limits

        config = _load_architect_config()
        for model_id in config.model_fallbacks:
            limits = get_model_limits(model_id)
            assert limits is not None, f"Fallback model '{model_id}' not in registry"
            assert limits.max_input_tokens > 0

    def test_manifest_write_path_allowlist(self):
        """Every allowed write path is confined to the agent's memory dir.

        This asserted one exact list — the state of a single instance on the
        day it was written — while `docs/agents/*.yaml` is instance config by
        rule 11. The module only RUNS where those manifests exist, so the
        assertion could never fire in CI and was guaranteed to fail on any
        instance that legitimately added a path. This one had added
        `watchdog_log.md` and `autodream_log.md`, both of which the
        memory-blocks test directly above already expects, and it had been
        red on the box ever since.

        The property worth pinning is not which files an operator chose but
        that none of them escapes: relative, under `brain/memory/`, no
        traversal, no absolute paths. That still fails loudly on a dangerous
        widening, which is what the guardrail is for.
        """
        manifest = _load_manifest_checked(MANIFEST_PATH)
        allowlist = manifest["v2"]["write_path_allowlist"]

        assert allowlist, "the write-path guardrail must not be empty"
        assert "brain/memory/agent-architect-status.md" in allowlist, (
            "the architect's own status file is what the guardrail exists to permit"
        )
        for entry in allowlist:
            assert not Path(entry).is_absolute(), f"absolute write path: {entry}"
            assert ".." not in Path(entry).parts, f"path traversal in write path: {entry}"
            assert entry.startswith("brain/memory/"), (
                f"write path escapes the agent's memory directory: {entry}"
            )

    def test_manifest_cost_budget(self):
        """The loaded cost cap matches the manifest's declared value.

        Asserted against ``v2.max_cost_usd`` in the manifest (default 0.0 =
        unlimited) rather than a hardcoded literal, so re-tuning the cap doesn't
        break this loader check. (audit 2026-05-29)
        """
        manifest = _load_manifest_checked(MANIFEST_PATH)
        config = _load_architect_config()
        declared = float(manifest.get("v2", {}).get("max_cost_usd", 0.0))
        assert config.max_cost_usd == declared
        assert config.max_cost_usd >= 0.0


# ═══════════════════════════════════════════════════════════════════
# Integration with Existing Systems
# ═══════════════════════════════════════════════════════════════════


class TestSystemIntegration:
    """Verify wiring to AutoAgent, AutoResearch, and scheduler."""

    def test_auto_agent_receives_architect_tasks(self):
        """auto-agent.yaml includes agent-architect in receives_tasks_from."""
        manifest = _load_manifest_checked(AUTO_AGENT_MANIFEST_PATH)
        assert "agent-architect" in manifest["receives_tasks_from"]

    def test_auto_researcher_receives_architect_tasks(self):
        """auto-researcher.yaml includes agent-architect in receives_tasks_from."""
        manifest = _load_manifest_checked(AUTO_RESEARCHER_MANIFEST_PATH)
        assert "agent-architect" in manifest["receives_tasks_from"]

    def test_scheduler_registers_cron(self):
        """Cron matches the manifest and builds a valid scheduler trigger.

        Asserted against the manifest's ``schedule.cron`` rather than a
        hardcoded literal — schedule changes are routine instance config and
        must not break this structural check. (audit 2026-05-29)
        """
        manifest = _load_manifest_checked(MANIFEST_PATH)
        config = _load_architect_config()
        assert config.cron_expr == manifest["schedule"]["cron"]
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(config.cron_expr)
        assert trigger is not None


# ═══════════════════════════════════════════════════════════════════
# Model Registry
# ═══════════════════════════════════════════════════════════════════


class TestModelRegistry:
    """Verify new models are registered with correct pricing."""

    def test_opus_4_7_registered(self):
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits("openrouter/anthropic/claude-opus-4.7")
        assert limits.max_input_tokens == 1_000_000
        assert limits.supports_thinking is True
        assert limits.input_cost_per_token == 0.000_005  # $5/M

    def test_gemini_3_1_pro_registered(self):
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits("openrouter/google/gemini-3.1-pro-preview")
        assert limits.max_input_tokens == 1_000_000
        assert limits.input_cost_per_token == 0.000_002  # $2/M

    def test_gpt_5_4_registered(self):
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits("openrouter/openai/gpt-5.4")
        assert limits.max_input_tokens == 1_050_000
        assert limits.input_cost_per_token == 0.000_002_5  # $2.50/M
