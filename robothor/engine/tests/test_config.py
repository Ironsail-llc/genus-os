"""Tests for engine config loading and system prompt building."""

from __future__ import annotations

from pathlib import Path

from robothor.engine.config import (
    BOOTSTRAP_TOTAL_MAX_CHARS,
    EngineConfig,
    _resolve_env_vars,
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
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("ROBOTHOR_ENGINE_PORT", raising=False)
        config = EngineConfig.from_env()
        assert config.port == 18800
        from robothor.constants import DEFAULT_TENANT

        assert config.tenant_id == DEFAULT_TENANT
        assert config.max_iterations == 20
        assert config.bot_token == ""
        assert config.default_chat_id == ""

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

    def test_operator_name_default(self, monkeypatch):
        """operator_name defaults to empty string."""
        monkeypatch.delenv("ROBOTHOR_OPERATOR_NAME", raising=False)
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = EngineConfig.from_env()
        assert config.operator_name == ""

    def test_operator_name_from_env(self, monkeypatch):
        """operator_name reads from ROBOTHOR_OPERATOR_NAME."""
        monkeypatch.setenv("ROBOTHOR_OPERATOR_NAME", "Alice")
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = EngineConfig.from_env()
        assert config.operator_name == "Alice"


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
                    "to": "99999999",
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
        assert config.heartbeat.delivery_to == "99999999"
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
        parts = build_system_prompt(config, tmp_path)
        prompt = parts.full_text()
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
        parts = build_system_prompt(config, tmp_path)
        prompt = parts.full_text()
        assert "Main instructions." in prompt
        assert "Shared context." in prompt
        assert "---" in prompt  # separator

    def test_missing_instruction_file(self, tmp_path):
        config = AgentConfig(id="t", name="t", instruction_file="missing.md")
        parts = build_system_prompt(config, tmp_path)
        prompt = parts.full_text()
        # Only the time context is present when instruction file is missing
        assert "Current time:" in prompt

    def test_large_file_loads_fully(self, tmp_path):
        """Large instruction files are loaded in full — never truncated."""
        big_content = "HEADER\n" + "x" * 20_000 + "\nFOOTER"
        (tmp_path / "big.md").write_text(big_content)
        config = AgentConfig(id="t", name="t", instruction_file="big.md")
        parts = build_system_prompt(config, tmp_path)
        prompt = parts.full_text()
        assert "HEADER" in prompt
        assert "FOOTER" in prompt  # Would be lost under old truncation
        assert "Current time:" in prompt

    def test_total_limit_raises(self, tmp_path):
        """Exceeding the total char limit raises ValueError, not silent truncation."""
        import pytest

        overflow = BOOTSTRAP_TOTAL_MAX_CHARS // 2 + 100
        (tmp_path / "instr.md").write_text("i" * overflow)
        (tmp_path / "bs1.md").write_text("b" * overflow)
        config = AgentConfig(
            id="t",
            name="t",
            instruction_file="instr.md",
            bootstrap_files=["bs1.md"],
        )
        with pytest.raises(ValueError, match="system prompt is .* chars"):
            build_system_prompt(config, tmp_path)

    def test_no_files(self):
        config = AgentConfig(id="t", name="t")
        parts = build_system_prompt(config, Path("/nonexistent"))
        prompt = parts.full_text()
        # Only time context when no files exist
        assert "Current time:" in prompt
        assert "UTC offset:" in prompt


class TestResolveEnvVars:
    def test_expands_string(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        assert _resolve_env_vars("${MY_VAR}") == "hello"

    def test_missing_var_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert _resolve_env_vars("${MISSING_VAR}") == ""

    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("CHAT_ID", "12345")
        data = {"delivery": {"to": "${CHAT_ID}", "mode": "announce"}}
        result = _resolve_env_vars(data)
        assert isinstance(result, dict)
        delivery = result["delivery"]
        assert isinstance(delivery, dict)
        assert delivery["to"] == "12345"
        assert delivery["mode"] == "announce"

    def test_nested_list(self, monkeypatch):
        monkeypatch.setenv("VAL", "resolved")
        data = ["${VAL}", "plain"]
        result = _resolve_env_vars(data)
        assert result == ["resolved", "plain"]

    def test_non_string_passthrough(self):
        assert _resolve_env_vars(42) == 42
        assert _resolve_env_vars(True) is True
        assert _resolve_env_vars(None) is None

    def test_manifest_env_var_expansion(self, tmp_path, monkeypatch):
        """load_manifest expands ${VAR} patterns from env vars."""
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "99999999")
        manifest = tmp_path / "test.yaml"
        manifest.write_text('id: test\ndelivery:\n  to: "${ROBOTHOR_TELEGRAM_CHAT_ID}"\n')
        data = load_manifest(manifest)
        assert data is not None
        assert data["delivery"]["to"] == "99999999"


class TestDeliveryToFallbackChain:
    """delivery_to falls back from ROBOTHOR_TELEGRAM_CHAT_ID to TELEGRAM_CHAT_ID."""

    def test_agent_delivery_to_prefers_prefixed_var(self, monkeypatch):
        """ROBOTHOR_TELEGRAM_CHAT_ID takes priority over TELEGRAM_CHAT_ID."""
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "111")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "222")
        config = manifest_to_agent_config({"id": "test", "delivery": {"mode": "announce"}})
        assert config.delivery_to == "111"

    def test_agent_delivery_to_falls_back_to_unprefixed(self, monkeypatch):
        """When ROBOTHOR_TELEGRAM_CHAT_ID is missing, TELEGRAM_CHAT_ID is used."""
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "222")
        config = manifest_to_agent_config({"id": "test", "delivery": {"mode": "announce"}})
        assert config.delivery_to == "222"

    def test_agent_delivery_to_manifest_value_wins(self, monkeypatch):
        """Explicit delivery.to in manifest overrides env vars."""
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "111")
        manifest = {"id": "test", "delivery": {"mode": "announce", "to": "999"}}
        config = manifest_to_agent_config(manifest)
        assert config.delivery_to == "999"

    def test_heartbeat_delivery_to_falls_back_to_unprefixed(self, monkeypatch):
        """Heartbeat delivery_to uses TELEGRAM_CHAT_ID when prefixed is missing."""
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "333")
        manifest = {
            "id": "main",
            "heartbeat": {
                "cron": "0 * * * *",
                "delivery": {"mode": "announce", "channel": "telegram"},
            },
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is not None
        assert config.heartbeat.delivery_to == "333"

    def test_heartbeat_delivery_to_prefers_prefixed(self, monkeypatch):
        """Heartbeat delivery_to prefers ROBOTHOR_TELEGRAM_CHAT_ID."""
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "444")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
        manifest = {
            "id": "main",
            "heartbeat": {
                "cron": "0 * * * *",
                "delivery": {"mode": "announce"},
            },
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is not None
        assert config.heartbeat.delivery_to == "444"

    def test_heartbeat_delivery_to_manifest_wins(self, monkeypatch):
        """Explicit heartbeat delivery.to overrides env vars."""
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "444")
        manifest = {
            "id": "main",
            "heartbeat": {
                "cron": "0 * * * *",
                "delivery": {"mode": "announce", "to": "777"},
            },
        }
        config = manifest_to_agent_config(manifest)
        assert config.heartbeat is not None
        assert config.heartbeat.delivery_to == "777"

    def test_both_missing_gives_empty(self, monkeypatch):
        """When both env vars are missing, delivery_to is empty."""
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        config = manifest_to_agent_config({"id": "test", "delivery": {"mode": "announce"}})
        assert config.delivery_to == ""


# ── Goals parsing ──────────────────────────────────────────────────────


class TestGoalsParsing:
    """Tests for parsing goals: section from agent manifests."""

    def test_goals_parsed_from_manifest(self):
        manifest = {
            "id": "email-classifier",
            "goals": [
                {
                    "id": "high-completion",
                    "metric": "completion_rate",
                    "target": ">0.95",
                    "weight": 1.0,
                },
                {
                    "id": "fast-classification",
                    "metric": "avg_duration_ms",
                    "target": "<1800000",
                    "weight": 0.5,
                },
            ],
        }
        config = manifest_to_agent_config(manifest)
        assert len(config.goals) == 2
        assert config.goals[0]["id"] == "high-completion"
        assert config.goals[0]["metric"] == "completion_rate"
        assert config.goals[0]["target"] == ">0.95"
        assert config.goals[1]["weight"] == 0.5

    def test_no_goals_defaults_to_empty(self):
        config = manifest_to_agent_config({"id": "bare"})
        assert config.goals == []

    def test_goals_empty_list(self):
        config = manifest_to_agent_config({"id": "bare", "goals": []})
        assert config.goals == []
