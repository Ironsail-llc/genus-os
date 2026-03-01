"""Tests for engine config loading and system prompt building."""

from __future__ import annotations

from pathlib import Path

from robothor.engine.config import (
    BOOTSTRAP_MAX_CHARS_PER_FILE,
    BOOTSTRAP_TOTAL_MAX_CHARS,
    EngineConfig,
    build_system_prompt,
    load_agent_config,
    load_all_manifests,
    load_manifest,
    manifest_to_agent_config,
)
from robothor.engine.models import AgentConfig, AgentHook, DeliveryMode, HeartbeatConfig


class TestEngineConfig:
    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("ROBOTHOR_ENGINE_PORT", raising=False)
        config = EngineConfig.from_env()
        assert config.port == 18800
        assert config.tenant_id == "robothor-primary"
        assert config.max_iterations == 20
        assert config.bot_token == ""

    def test_from_env_custom(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", "my-token")
        monkeypatch.setenv("ROBOTHOR_ENGINE_PORT", "19000")
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "custom-tenant")
        config = EngineConfig.from_env()
        assert config.bot_token == "my-token"
        assert config.port == 19000
        assert config.tenant_id == "custom-tenant"

    def test_default_chat_agent_default(self, monkeypatch):
        """default_chat_agent defaults to 'main'."""
        monkeypatch.delenv("ROBOTHOR_DEFAULT_CHAT_AGENT", raising=False)
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = EngineConfig.from_env()
        assert config.default_chat_agent == "main"

    def test_default_chat_agent_from_env(self, monkeypatch):
        """default_chat_agent reads from ROBOTHOR_DEFAULT_CHAT_AGENT."""
        monkeypatch.setenv("ROBOTHOR_DEFAULT_CHAT_AGENT", "custom-chat")
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = EngineConfig.from_env()
        assert config.default_chat_agent == "custom-chat"


class TestManifestLoading:
    def test_load_manifest_valid(self, sample_manifest):
        path = sample_manifest / "test-agent.yaml"
        data = load_manifest(path)
        assert data is not None
        assert data["id"] == "test-agent"
        assert data["name"] == "Test Agent"

    def test_load_manifest_missing(self, tmp_path):
        result = load_manifest(tmp_path / "nonexistent.yaml")
        assert result is None

    def test_load_manifest_invalid_yaml(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: valid: yaml: [")
        result = load_manifest(bad)
        assert result is None

    def test_load_manifest_no_id(self, tmp_path):
        no_id = tmp_path / "no-id.yaml"
        no_id.write_text("name: No ID Agent\ndescription: Missing ID field\n")
        result = load_manifest(no_id)
        assert result is None

    def test_load_all_manifests(self, sample_manifest):
        manifests = load_all_manifests(sample_manifest)
        assert len(manifests) == 1
        assert manifests[0]["id"] == "test-agent"

    def test_load_all_manifests_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert load_all_manifests(empty) == []

    def test_load_all_manifests_nonexistent(self, tmp_path):
        assert load_all_manifests(tmp_path / "nope") == []


class TestManifestToAgentConfig:
    def test_basic_conversion(self):
        manifest = {
            "id": "my-agent",
            "name": "My Agent",
            "description": "Test",
            "model": {
                "primary": "openrouter/test/model",
                "fallbacks": ["openrouter/test/fb1"],
            },
            "schedule": {
                "cron": "*/30 * * * *",
                "timezone": "UTC",
                "timeout_seconds": 300,
            },
            "delivery": {
                "mode": "announce",
                "channel": "telegram",
                "to": "12345",
            },
            "tools_allowed": ["list_tasks"],
            "tools_denied": ["message"],
            "instruction_file": "brain/MY_AGENT.md",
            "bootstrap_files": ["brain/AGENTS.md"],
            "task_protocol": True,
            "sla": {"urgent": "30m"},
        }
        config = manifest_to_agent_config(manifest)
        assert config.id == "my-agent"
        assert config.model_primary == "openrouter/test/model"
        assert config.model_fallbacks == ["openrouter/test/fb1"]
        assert config.cron_expr == "*/30 * * * *"
        assert config.delivery_mode == DeliveryMode.ANNOUNCE
        assert config.delivery_to == "12345"
        assert config.tools_allowed == ["list_tasks"]
        assert config.tools_denied == ["message"]
        assert config.task_protocol is True
        assert config.sla == {"urgent": "30m"}

    def test_minimal_manifest(self):
        config = manifest_to_agent_config({"id": "bare"})
        assert config.id == "bare"
        assert config.name == "bare"
        assert config.model_primary == ""
        assert config.delivery_mode == DeliveryMode.NONE
        assert config.tools_allowed == []

    def test_invalid_delivery_mode(self):
        config = manifest_to_agent_config({"id": "x", "delivery": {"mode": "invalid_mode"}})
        assert config.delivery_mode == DeliveryMode.NONE

    def test_max_iterations_from_manifest(self):
        """max_iterations is read from the schedule section."""
        manifest = {
            "id": "limited-agent",
            "schedule": {"max_iterations": 8},
        }
        config = manifest_to_agent_config(manifest)
        assert config.max_iterations == 8

    def test_max_iterations_default(self):
        """max_iterations defaults to 20 when not specified."""
        config = manifest_to_agent_config({"id": "bare"})
        assert config.max_iterations == 20

    def test_hooks_parsed_from_manifest(self):
        """Hooks are parsed from manifest into AgentHook dataclasses."""
        manifest = {
            "id": "hooked-agent",
            "hooks": [
                {"stream": "email", "event_type": "email.new", "message": "New mail"},
                {"stream": "calendar", "event_type": "calendar.new"},
            ],
        }
        config = manifest_to_agent_config(manifest)
        assert len(config.hooks) == 2
        assert isinstance(config.hooks[0], AgentHook)
        assert config.hooks[0].stream == "email"
        assert config.hooks[0].event_type == "email.new"
        assert config.hooks[0].message == "New mail"
        assert config.hooks[1].message == ""  # Default

    def test_hooks_invalid_entries_skipped(self):
        """Invalid hook entries are silently skipped."""
        manifest = {
            "id": "bad-hooks",
            "hooks": [
                {"stream": "email"},  # Missing event_type
                {"event_type": "email.new"},  # Missing stream
                "not-a-dict",
                {"stream": "email", "event_type": "email.new", "message": "Valid"},
            ],
        }
        config = manifest_to_agent_config(manifest)
        assert len(config.hooks) == 1
        assert config.hooks[0].event_type == "email.new"

    def test_hooks_empty_list(self):
        """Empty hooks list produces empty hooks field."""
        config = manifest_to_agent_config({"id": "bare", "hooks": []})
        assert config.hooks == []

    def test_hooks_missing(self):
        """No hooks field produces empty hooks field."""
        config = manifest_to_agent_config({"id": "bare"})
        assert config.hooks == []

    def test_heartbeat_parsed_from_manifest(self):
        """Heartbeat section creates HeartbeatConfig on AgentConfig."""
        manifest = {
            "id": "main",
            "name": "Robothor",
            "schedule": {"timezone": "America/New_York"},
            "heartbeat": {
                "cron": "0 6-22/4 * * *",
                "instruction_file": "brain/HEARTBEAT.md",
                "session_target": "isolated",
                "max_iterations": 15,
                "timeout_seconds": 600,
                "delivery": {
                    "mode": "announce",
                    "channel": "telegram",
                    "to": "7636850023",
                },
                "context_files": ["brain/memory/status.md"],
                "peer_agents": ["email-classifier"],
                "bootstrap_files": ["brain/AGENTS.md"],
            },
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is not None
        assert isinstance(config.heartbeat, HeartbeatConfig)
        assert config.heartbeat.cron_expr == "0 6-22/4 * * *"
        assert config.heartbeat.instruction_file == "brain/HEARTBEAT.md"
        assert config.heartbeat.session_target == "isolated"
        assert config.heartbeat.max_iterations == 15
        assert config.heartbeat.timeout_seconds == 600
        assert config.heartbeat.delivery_mode == DeliveryMode.ANNOUNCE
        assert config.heartbeat.delivery_channel == "telegram"
        assert config.heartbeat.delivery_to == "7636850023"
        assert config.heartbeat.warmup_context_files == ["brain/memory/status.md"]
        assert config.heartbeat.warmup_peer_agents == ["email-classifier"]
        assert config.heartbeat.bootstrap_files == ["brain/AGENTS.md"]
        # token_budget is auto-derived at runtime, not parsed from YAML
        assert config.heartbeat.token_budget == 0

    def test_heartbeat_missing_is_none(self):
        """No heartbeat key → None."""
        config = manifest_to_agent_config({"id": "bare"})
        assert config.heartbeat is None

    def test_heartbeat_without_cron_is_none(self):
        """Heartbeat without cron → None."""
        manifest = {
            "id": "test",
            "heartbeat": {
                "instruction_file": "brain/HEARTBEAT.md",
            },
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is None

    def test_heartbeat_inherits_timezone(self):
        """Heartbeat inherits timezone from schedule when not specified."""
        manifest = {
            "id": "test",
            "schedule": {"timezone": "US/Eastern"},
            "heartbeat": {"cron": "0 * * * *"},
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is not None
        assert config.heartbeat.timezone == "US/Eastern"

    def test_heartbeat_overrides_timezone(self):
        """Heartbeat can override timezone from schedule."""
        manifest = {
            "id": "test",
            "schedule": {"timezone": "US/Eastern"},
            "heartbeat": {"cron": "0 * * * *", "timezone": "UTC"},
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is not None
        assert config.heartbeat.timezone == "UTC"


class TestLoadAgentConfig:
    def test_load_by_filename(self, sample_manifest):
        config = load_agent_config("test-agent", sample_manifest)
        assert config is not None
        assert config.id == "test-agent"
        assert config.model_primary == "openrouter/test/model"

    def test_load_nonexistent(self, tmp_path):
        empty = tmp_path / "agents"
        empty.mkdir()
        assert load_agent_config("nonexistent", empty) is None


class TestBuildSystemPrompt:
    def test_instruction_only(self, tmp_path):
        (tmp_path / "instructions.md").write_text("You are a test agent.")
        config = AgentConfig(id="t", name="t", instruction_file="instructions.md")
        prompt = build_system_prompt(config, tmp_path)
        assert "You are a test agent." in prompt

    def test_instruction_plus_bootstrap(self, tmp_path):
        (tmp_path / "instr.md").write_text("Main instructions.")
        (tmp_path / "shared.md").write_text("Shared context.")
        config = AgentConfig(
            id="t",
            name="t",
            instruction_file="instr.md",
            bootstrap_files=["shared.md"],
        )
        prompt = build_system_prompt(config, tmp_path)
        assert "Main instructions." in prompt
        assert "Shared context." in prompt
        assert "---" in prompt  # separator

    def test_missing_instruction_file(self, tmp_path):
        config = AgentConfig(id="t", name="t", instruction_file="missing.md")
        prompt = build_system_prompt(config, tmp_path)
        assert prompt == ""

    def test_truncation(self, tmp_path):
        big_content = "x" * (BOOTSTRAP_MAX_CHARS_PER_FILE + 1000)
        (tmp_path / "big.md").write_text(big_content)
        config = AgentConfig(id="t", name="t", instruction_file="big.md")
        prompt = build_system_prompt(config, tmp_path)
        assert len(prompt) == BOOTSTRAP_MAX_CHARS_PER_FILE

    def test_total_limit(self, tmp_path):
        # Create files that together exceed total limit
        half = BOOTSTRAP_TOTAL_MAX_CHARS // 2 + 100
        (tmp_path / "instr.md").write_text("i" * half)
        (tmp_path / "bs1.md").write_text("b" * half)
        (tmp_path / "bs2.md").write_text("c" * half)
        config = AgentConfig(
            id="t",
            name="t",
            instruction_file="instr.md",
            bootstrap_files=["bs1.md", "bs2.md"],
        )
        prompt = build_system_prompt(config, tmp_path)
        # Should be under total limit (separators add a few chars)
        assert len(prompt) <= BOOTSTRAP_TOTAL_MAX_CHARS + 100  # separator overhead

    def test_no_files(self):
        config = AgentConfig(id="t", name="t")
        prompt = build_system_prompt(config, Path("/nonexistent"))
        assert prompt == ""
