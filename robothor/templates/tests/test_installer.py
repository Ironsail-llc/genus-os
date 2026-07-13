"""Tests for the agent template installer."""

import pytest
import yaml

from robothor.templates.installer import import_agent, install, remove, update
from robothor.templates.instance import InstanceConfig
from robothor.templates.safety import TemplateSecurityError


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
        assert manifest["model"]["primary"] == "openrouter/xiaomi/mimo-v2-pro"

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

    def test_remote_install_records_integrity_provenance(
        self, tmp_bundle, tmp_repo, tmp_instance_dir
    ):
        install(
            tmp_bundle,
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
            source="hub",
            source_ref="test-agent",
            source_sha256="a" * 64,
        )

        record = InstanceConfig.load(tmp_instance_dir).installed_agents["test-agent"]
        assert record["source"] == "hub"
        assert record["source_path"] == "test-agent"
        assert record["source_sha256"] == "a" * 64

    def test_remote_install_requires_integrity_before_writes(
        self, tmp_bundle, tmp_repo, tmp_instance_dir
    ):
        with pytest.raises(TemplateSecurityError, match="SHA-256"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
                source="hub",
                source_ref="test-agent",
            )
        assert not (tmp_repo / "docs" / "agents" / "test-agent.yaml").exists()

    def test_remote_install_rejects_registry_slug_mismatch(
        self, tmp_bundle, tmp_repo, tmp_instance_dir
    ):
        with pytest.raises(TemplateSecurityError, match="registry slug"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
                source="hub",
                source_ref="different-agent",
                source_sha256="a" * 64,
            )
        assert not (tmp_repo / "docs" / "agents" / "test-agent.yaml").exists()

    def test_install_rejects_symlinked_bundle_root(
        self, tmp_bundle, tmp_repo, tmp_instance_dir, tmp_path
    ):
        alias = tmp_path / "bundle-alias"
        alias.symlink_to(tmp_bundle, target_is_directory=True)

        with pytest.raises(FileNotFoundError, match="unsafe"):
            install(
                alias,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )

    def test_install_missing_bundle(self, tmp_repo, tmp_instance_dir):
        """Install with nonexistent path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            install(
                "/nonexistent",
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )

    @pytest.mark.parametrize("agent_id", ["../escape", "/absolute", "UPPER", "a/b"])
    def test_install_rejects_unsafe_agent_id(
        self, agent_id, tmp_bundle, tmp_repo, tmp_instance_dir
    ):
        setup_path = tmp_bundle / "setup.yaml"
        setup = yaml.safe_load(setup_path.read_text())
        setup["agent_id"] = agent_id
        setup_path.write_text(yaml.safe_dump(setup))

        with pytest.raises(TemplateSecurityError, match="agent ID"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )

    @pytest.mark.parametrize(
        "instruction_path", ["../../victim.md", "/tmp/victim.md", "brain/../victim.md"]
    )
    def test_install_rejects_escaping_instruction_path(
        self, instruction_path, tmp_bundle, tmp_repo, tmp_instance_dir
    ):
        setup_path = tmp_bundle / "setup.yaml"
        setup = yaml.safe_load(setup_path.read_text())
        setup["instruction_file_path"] = instruction_path
        setup_path.write_text(yaml.safe_dump(setup))

        with pytest.raises(TemplateSecurityError, match="instruction"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )

    def test_install_rejects_malicious_manifest_id(self, tmp_bundle, tmp_repo, tmp_instance_dir):
        manifest_path = tmp_bundle / "manifest.template.yaml"
        manifest_path.write_text(
            manifest_path.read_text().replace("id: test-agent", "id: ../escape")
        )

        with pytest.raises(TemplateSecurityError, match="manifest agent ID"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )

    def test_install_rejects_symlink_escape(self, tmp_bundle, tmp_repo, tmp_instance_dir, tmp_path):
        victim = tmp_path / "victim.md"
        victim.write_text("do not overwrite")
        instruction = tmp_repo / "brain" / "TEST_AGENT.md"
        instruction.unlink()
        instruction.symlink_to(victim)

        with pytest.raises(TemplateSecurityError, match="symlinks"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )
        assert victim.read_text() == "do not overwrite"

    def test_install_rejects_manifest_destination_symlink(
        self, tmp_bundle, tmp_repo, tmp_instance_dir, tmp_path
    ):
        victim = tmp_path / "victim.yaml"
        victim.write_text("safe: true\n")
        (tmp_repo / "docs" / "agents" / "test-agent.yaml").symlink_to(victim)

        with pytest.raises(TemplateSecurityError, match="symlinks"):
            install(
                tmp_bundle,
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )
        assert victim.read_text() == "safe: true\n"


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

    def test_remove_ignores_persisted_file_paths(
        self, tmp_bundle, tmp_repo, tmp_instance_dir, tmp_path
    ):
        install(
            tmp_bundle,
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        victim = tmp_path / "outside.txt"
        victim.write_text("must survive")
        installed_path = tmp_instance_dir / "installed.yaml"
        registry = yaml.safe_load(installed_path.read_text())
        registry["agents"]["test-agent"]["files"] = {
            "manifest": str(victim),
            "instruction": "../../outside.txt",
        }
        installed_path.write_text(yaml.safe_dump(registry))

        assert remove("test-agent", instance_dir=tmp_instance_dir, repo_root=tmp_repo)
        assert victim.read_text() == "must survive"
        assert not (tmp_repo / "docs" / "agents" / "test-agent.yaml").exists()


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
            new_template_path=tmp_bundle,
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

    def test_update_never_executes_persisted_source_path(
        self, tmp_bundle, tmp_repo, tmp_instance_dir, tmp_path
    ):
        install(
            tmp_bundle,
            auto_yes=True,
            instance_dir=tmp_instance_dir,
            repo_root=tmp_repo,
        )
        registry_path = tmp_instance_dir / "installed.yaml"
        registry = yaml.safe_load(registry_path.read_text())
        registry["agents"]["test-agent"]["source_path"] = str(tmp_path / "untrusted")
        registry_path.write_text(yaml.safe_dump(registry))

        with pytest.raises(TemplateSecurityError, match="explicit update template"):
            update(
                "test-agent",
                auto_yes=True,
                instance_dir=tmp_instance_dir,
                repo_root=tmp_repo,
            )


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
                "primary": "openrouter/xiaomi/mimo-v2-pro",
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

    def test_import_rejects_escaping_manifest_instruction(self, tmp_repo, tmp_path):
        manifest_path = tmp_repo / "docs" / "agents" / "bad-agent.yaml"
        manifest_path.write_text(
            yaml.safe_dump(
                {
                    "id": "bad-agent",
                    "name": "Bad Agent",
                    "description": "bad path",
                    "version": "1.0.0",
                    "department": "custom",
                    "instruction_file": "../../victim.md",
                }
            )
        )

        output = tmp_path / "output"
        with pytest.raises(TemplateSecurityError, match="instruction"):
            import_agent("bad-agent", output_dir=output, repo_root=tmp_repo)
        assert not output.exists()
