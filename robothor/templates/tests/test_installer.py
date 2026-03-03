"""Tests for the agent template installer."""

import pytest
import yaml

from robothor.templates.installer import import_agent, install, remove, update
from robothor.templates.instance import InstanceConfig


class TestInstall:
    def test_install_basic(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        """Install a template bundle and verify output files."""
        result = install(
            str(tmp_bundle),
            overrides={"version": "1.0.0"},
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        assert result["agent_id"] == "test-agent"
        assert result["version"] == "1.0.0"
        assert "manifest" in result["files"]

        # Verify manifest was written
        manifest_path = tmp_repo / "docs" / "agents" / "test-agent.yaml"
        assert manifest_path.exists()
        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["id"] == "test-agent"
        assert manifest["model"]["primary"] == "openrouter/moonshotai/kimi-k2.5"

    def test_install_with_overrides(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        """Install with variable overrides."""
        install(
            str(tmp_bundle),
            overrides={
                "version": "2.0.0",
                "model_primary": "openrouter/anthropic/claude-sonnet-4.6",
            },
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        manifest_path = tmp_repo / "docs" / "agents" / "test-agent.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["model"]["primary"] == "openrouter/anthropic/claude-sonnet-4.6"

    def test_install_records_in_installed_yaml(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        """Install records the agent in installed.yaml."""
        install(
            str(tmp_bundle),
            overrides={"version": "1.0.0"},
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        instance = InstanceConfig.load(tmp_instance_dir)
        agents = instance.installed_agents
        assert "test-agent" in agents
        assert agents["test-agent"]["source"] == "local"

    def test_install_missing_bundle(self, tmp_repo, tmp_instance_dir):
        """Install with nonexistent path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            install(
                "/nonexistent",
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )


class TestRemove:
    def test_remove_installed(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        """Remove a previously installed agent."""
        install(
            str(tmp_bundle),
            overrides={"version": "1.0.0"},
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        # Verify files exist
        manifest_path = tmp_repo / "docs" / "agents" / "test-agent.yaml"
        assert manifest_path.exists()

        # Remove
        assert remove("test-agent", instance_dir=tmp_instance_dir, repo_root=tmp_repo)

        # Verify files removed
        assert not manifest_path.exists()

        # Verify removed from installed.yaml
        instance = InstanceConfig.load(tmp_instance_dir)
        assert "test-agent" not in instance.installed_agents

    def test_remove_nonexistent(self, tmp_instance_dir, tmp_repo):
        """Remove returns False for unknown agent."""
        assert not remove("nonexistent", instance_dir=tmp_instance_dir, repo_root=tmp_repo)

    def test_remove_with_archive(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        """Remove with archive=True preserves files."""
        install(
            str(tmp_bundle),
            overrides={"version": "1.0.0"},
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        assert remove(
            "test-agent",
            archive=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        # Archive should exist
        archive = tmp_instance_dir / "archive" / "test-agent"
        assert archive.exists()


class TestUpdate:
    def test_update_agent(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        """Update re-resolves the template with new overrides."""
        # First install
        install(
            str(tmp_bundle),
            overrides={"version": "1.0.0"},
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )

        # Update with new model
        result = update(
            "test-agent",
            overrides={"model_primary": "new-model"},
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        assert result is not None

        # Verify updated manifest
        manifest_path = tmp_repo / "docs" / "agents" / "test-agent.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["model"]["primary"] == "new-model"

    def test_update_nonexistent(self, tmp_instance_dir, tmp_repo):
        """Update returns None for unknown agent."""
        result = update(
            "nonexistent",
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        assert result is None


class TestImport:
    def test_import_existing_agent(self, tmp_repo, tmp_path):
        """Import an existing manifest into a template bundle."""
        # Create a fake existing manifest
        agents_dir = tmp_repo / "docs" / "agents"
        manifest = {
            "id": "my-agent",
            "name": "My Agent",
            "description": "A test agent",
            "version": "2026-03-01",
            "department": "custom",
            "model": {
                "primary": "openrouter/moonshotai/kimi-k2.5",
                "fallbacks": ["gemini/gemini-2.5-pro"],
            },
            "schedule": {
                "cron": "0 */4 * * *",
                "timezone": "America/New_York",
                "timeout_seconds": 300,
                "max_iterations": 10,
            },
            "delivery": {"mode": "none"},
            "reports_to": "main",
            "escalates_to": "main",
            "instruction_file": "brain/MY_AGENT.md",
            "bootstrap_files": [],
        }
        (agents_dir / "my-agent.yaml").write_text(yaml.dump(manifest, default_flow_style=False))
        # Create instruction file
        (tmp_repo / "brain" / "MY_AGENT.md").write_text("# My Agent\n")

        # Import
        output_dir = tmp_path / "output"
        result = import_agent(
            "my-agent",
            output_dir=str(output_dir),
            repo_root=tmp_repo,
        )
        assert result["agent_id"] == "my-agent"
        assert (output_dir / "manifest.template.yaml").exists()
        assert (output_dir / "setup.yaml").exists()
        assert (output_dir / "SKILL.md").exists()
        assert (output_dir / "programmatic.json").exists()

    def test_import_nonexistent(self, tmp_repo, tmp_path):
        """Import raises FileNotFoundError for missing agent."""
        with pytest.raises(FileNotFoundError):
            import_agent(
                "nonexistent",
                output_dir=str(tmp_path / "output"),
                repo_root=tmp_repo,
            )
