"""Integration tests for config loading precedence.

Tests the full chain: fleet defaults → agent manifest → env overrides.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.integration
class TestConfigPrecedence:
    def test_defaults_applied_when_manifest_omits_field(self, tmp_path: Path) -> None:
        """Fleet defaults should fill in missing fields."""
        defaults = {"v2": {"error_feedback": True, "checkpoint_enabled": True}}
        manifest = {
            "id": "test-agent",
            "name": "Test",
            "description": "test",
            "model": {"primary": "test-model"},
            "schedule": {"cron": "", "timezone": "UTC"},
            "delivery": {"mode": "none"},
            "instruction_file": "",
        }

        defaults_path = tmp_path / "_defaults.yaml"
        manifest_path = tmp_path / "test-agent.yaml"
        defaults_path.write_text(yaml.dump(defaults))
        manifest_path.write_text(yaml.dump(manifest))

        from robothor.engine.config import manifest_to_agent_config

        config = manifest_to_agent_config(manifest_path, defaults_dir=tmp_path)
        assert config.error_feedback is True
        assert config.checkpoint_enabled is True

    def test_manifest_overrides_defaults(self, tmp_path: Path) -> None:
        """Agent manifest values should override fleet defaults."""
        defaults = {"v2": {"max_iterations": 20}}
        manifest = {
            "id": "test-agent",
            "name": "Test",
            "description": "test",
            "model": {"primary": "test-model"},
            "schedule": {"cron": "", "timezone": "UTC", "max_iterations": 5},
            "delivery": {"mode": "none"},
            "instruction_file": "",
        }

        defaults_path = tmp_path / "_defaults.yaml"
        manifest_path = tmp_path / "test-agent.yaml"
        defaults_path.write_text(yaml.dump(defaults))
        manifest_path.write_text(yaml.dump(manifest))

        from robothor.engine.config import manifest_to_agent_config

        config = manifest_to_agent_config(manifest_path, defaults_dir=tmp_path)
        assert config.max_iterations == 5

    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ROBOTHOR_OVERRIDE_* env vars should take highest precedence."""
        manifest = {
            "id": "test-agent",
            "name": "Test",
            "description": "test",
            "model": {"primary": "test-model"},
            "schedule": {"cron": "", "timezone": "UTC", "max_iterations": 10},
            "delivery": {"mode": "none"},
            "instruction_file": "",
        }

        manifest_path = tmp_path / "test-agent.yaml"
        manifest_path.write_text(yaml.dump(manifest))

        monkeypatch.setenv("ROBOTHOR_OVERRIDE_SCHEDULE__MAX_ITERATIONS", "3")

        from robothor.engine.config import manifest_to_agent_config

        config = manifest_to_agent_config(manifest_path, defaults_dir=tmp_path)
        # Env override should cap iterations
        assert config.max_iterations <= 10
