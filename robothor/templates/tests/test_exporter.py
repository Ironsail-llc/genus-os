"""`genus agent export` — the bundle an instance hands to another instance."""

from __future__ import annotations

import tarfile

import pytest
import yaml

from robothor.templates.bundle import (
    BUNDLE_FILENAME,
    BundleError,
    read_bundle,
    verify_bundle_files,
)
from robothor.templates.exporter import ExportError, export_agent

MANIFEST = """\
id: test-agent
name: Test Agent
description: A test agent
version: "1.0.0"
department: custom

model:
  primary: openrouter/xiaomi/mimo-v2-pro

schedule:
  cron: "0 * * * *"
  timezone: UTC

delivery:
  mode: none

tools_allowed: []
instruction_file: brain/TEST_AGENT.md
"""

INSTRUCTIONS = "# Test Agent\n\nYou triage things.\n"


def _install_agent(repo, *, manifest: str = MANIFEST, instructions: str = INSTRUCTIONS):
    (repo / "docs" / "agents" / "test-agent.yaml").write_text(manifest)
    (repo / "brain" / "TEST_AGENT.md").write_text(instructions)


def _add_skill(repo, name: str, body: str = "# Skill\n"):
    skill = repo / "agents" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(body)
    (skill / "reference.md").write_text("Details.\n")
    return skill


def _adapter_dir(tmp_path, name: str, document: dict):
    directory = tmp_path / "adapters"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.yaml").write_text(yaml.dump(document, sort_keys=False))
    return directory


class TestExportLayout:
    def test_writes_a_bundle_whose_hashes_verify(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo)
        out = tmp_path / "out"

        result = export_agent("test-agent", out=out, repo_root=tmp_repo)

        assert result.agent_id == "test-agent"
        assert not result.is_archive
        names = {p.name for p in out.iterdir()}
        assert {BUNDLE_FILENAME, "setup.yaml", "manifest.template.yaml"} <= names
        manifest = read_bundle(out)
        assert manifest.id == "test-agent"
        assert manifest.version == "1.0.0"
        verify_bundle_files(out, manifest)

    def test_bundle_yaml_is_not_one_of_its_own_files(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo)
        out = tmp_path / "out"
        export_agent("test-agent", out=out, repo_root=tmp_repo)
        assert BUNDLE_FILENAME not in read_bundle(out).file_paths()

    def test_copies_the_skills_the_manifest_requires(self, tmp_repo, tmp_path):
        _add_skill(tmp_repo, "triage")
        _install_agent(tmp_repo, manifest=MANIFEST + "\nrequires:\n  skills: [triage]\n")
        out = tmp_path / "out"

        manifest = export_agent("test-agent", out=out, repo_root=tmp_repo).manifest

        assert manifest.requires.skills == ("triage",)
        assert "skills/triage/SKILL.md" in manifest.file_paths()
        assert "skills/triage/reference.md" in manifest.file_paths()
        verify_bundle_files(out, manifest)

    def test_refuses_a_skill_the_instance_does_not_have(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo, manifest=MANIFEST + "\nrequires:\n  skills: [missing]\n")
        with pytest.raises(ExportError, match="missing"):
            export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo)

    def test_lists_the_env_names_the_manifest_references(self, tmp_repo, tmp_path):
        _install_agent(
            tmp_repo,
            manifest=MANIFEST.replace("  mode: none", '  mode: none\n  to: "${TEAM_CHAT_ID}"'),
        )
        manifest = export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo).manifest
        assert "TEAM_CHAT_ID" in manifest.requires.secrets


class TestArchive:
    def test_same_agent_exports_to_the_same_bytes(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo)
        stamp = "2026-09-15T00:00:00+00:00"

        first = tmp_path / "a.tar.gz"
        second = tmp_path / "b.tar.gz"
        export_agent("test-agent", out=first, repo_root=tmp_repo, exported_at=stamp)
        export_agent("test-agent", out=second, repo_root=tmp_repo, exported_at=stamp)

        assert first.read_bytes() == second.read_bytes()

    def test_archive_members_are_rooted_at_the_agent_id(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo)
        out = tmp_path / "agent.tar.gz"
        result = export_agent("test-agent", out=out, repo_root=tmp_repo)

        assert result.is_archive
        with tarfile.open(out, "r:gz") as archive:
            members = archive.getnames()
        assert f"test-agent/{BUNDLE_FILENAME}" in members
        assert all(name.startswith("test-agent/") for name in members)


class TestSecretGate:
    def test_refuses_an_api_key_in_the_instruction_file(self, tmp_repo, tmp_path):
        _install_agent(
            tmp_repo,
            instructions="# Test Agent\n\nRun with:\n\n```\nAPI_KEY=sk-or-v1-abcdefghijklmnop\n```\n",
        )
        out = tmp_path / "out"

        with pytest.raises(ExportError) as excinfo:
            export_agent("test-agent", out=out, repo_root=tmp_repo)

        message = str(excinfo.value)
        assert "instructions.template.md:6" in message
        assert "sk-or-v1-abcdefghijklmnop" not in message
        assert not out.exists()

    def test_refuses_a_credential_in_the_manifest(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo, manifest=MANIFEST + '\nclient_secret: "hunter2hunter2"\n')
        with pytest.raises(ExportError, match="manifest.template.yaml"):
            export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo)

    def test_writes_no_archive_when_it_refuses(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo, manifest=MANIFEST + '\nclient_secret: "hunter2hunter2"\n')
        out = tmp_path / "agent.tar.gz"
        with pytest.raises(ExportError):
            export_agent("test-agent", out=out, repo_root=tmp_repo)
        assert not out.exists()


class TestLeakGate:
    def test_refuses_a_home_path_in_a_skill(self, tmp_repo, tmp_path):
        _add_skill(tmp_repo, "triage", body="# Skill\n\nRead /home/alice/notes.md.\n")
        _install_agent(tmp_repo, manifest=MANIFEST + "\nrequires:\n  skills: [triage]\n")

        with pytest.raises(ExportError) as excinfo:
            export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo)

        assert "skills/triage/SKILL.md:3" in str(excinfo.value)

    def test_refuses_the_exporting_workspace_path(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo, instructions=f"# Test Agent\n\nWorkspace is {tmp_repo}.\n")
        with pytest.raises(ExportError, match="workspace"):
            export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo)


class TestAdapters:
    def test_adapters_are_listed_but_not_carried_by_default(self, tmp_repo, tmp_path):
        adapters = _adapter_dir(
            tmp_path,
            "billing",
            {
                "name": "billing",
                "transport": "http",
                "url": "https://example.com/_mcp",
                "agents": ["test-agent"],
            },
        )
        _install_agent(tmp_repo)
        out = tmp_path / "out"

        manifest = export_agent(
            "test-agent", out=out, repo_root=tmp_repo, adapter_dir=adapters
        ).manifest

        assert manifest.requires.adapters == ("billing",)
        assert not any(p.startswith("adapters/") for p in manifest.file_paths())

    def test_include_adapters_collapses_credentials_to_env_references(self, tmp_repo, tmp_path):
        adapters = _adapter_dir(
            tmp_path,
            "billing",
            {
                "name": "billing",
                "transport": "http",
                "url": "https://example.com/_mcp",
                "headers": {"Authorization": "Bearer xoxb-000000-secret-value"},
                "env": {"BILLING_API_KEY": "sk-live-abcdefghijklmnop"},
                "agents": ["test-agent"],
            },
        )
        _install_agent(tmp_repo)
        out = tmp_path / "out"

        result = export_agent(
            "test-agent",
            out=out,
            repo_root=tmp_repo,
            adapter_dir=adapters,
            include_adapters=True,
        )

        copied = yaml.safe_load((out / "adapters" / "billing.yaml").read_text())
        assert copied["headers"]["Authorization"] == "${BILLING_AUTHORIZATION}"
        assert copied["env"]["BILLING_API_KEY"] == "${BILLING_API_KEY}"
        assert "BILLING_AUTHORIZATION" in result.manifest.requires.secrets
        assert "BILLING_API_KEY" in result.manifest.requires.secrets
        assert "adapters/billing.yaml" in result.manifest.file_paths()
        verify_bundle_files(out, result.manifest)

    def test_ignores_an_adapter_that_serves_every_agent(self, tmp_repo, tmp_path):
        adapters = _adapter_dir(
            tmp_path,
            "shared",
            {
                "name": "shared",
                "transport": "http",
                "url": "https://x.example/_mcp",
                "agents": ["*"],
            },
        )
        _install_agent(tmp_repo)
        manifest = export_agent(
            "test-agent", out=tmp_path / "out", repo_root=tmp_repo, adapter_dir=adapters
        ).manifest
        assert manifest.requires.adapters == ()


class TestRefusals:
    def test_refuses_an_unknown_agent(self, tmp_repo, tmp_path):
        with pytest.raises(FileNotFoundError):
            export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo)

    def test_refuses_a_non_empty_output_directory(self, tmp_repo, tmp_path):
        _install_agent(tmp_repo)
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("mine\n")
        with pytest.raises(ExportError, match="empty"):
            export_agent("test-agent", out=out, repo_root=tmp_repo)

    def test_export_does_not_record_an_install(self, tmp_repo, tmp_path, monkeypatch):
        """Exporting is a read. It must not rewrite installed.yaml's source path."""
        _install_agent(tmp_repo)
        called = []
        from robothor.templates import instance as instance_module

        monkeypatch.setattr(
            instance_module.InstanceConfig,
            "record_install",
            lambda self, **kwargs: called.append(kwargs),
        )
        export_agent("test-agent", out=tmp_path / "out", repo_root=tmp_repo)
        assert called == []


class TestBundleReadBack:
    def test_a_directory_without_bundle_yaml_is_not_a_bundle(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "setup.yaml").write_text("agent_id: test-agent\n")
        with pytest.raises(BundleError, match=BUNDLE_FILENAME):
            read_bundle(plain)
