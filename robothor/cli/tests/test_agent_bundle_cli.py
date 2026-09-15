"""`genus agent export` and the bundle half of `genus agent install`.

The CLI contract is the part an operator actually touches, so the properties
here are the ones a wrong exit code would hide: a refused export exits 2 and
writes nothing, an install without ``--yes`` prints the plan and writes
nothing, and a directory that is a bundle takes the bundle path while a
directory that is a plain template still takes the old one.
"""

from __future__ import annotations

import tarfile

import pytest
import yaml

from robothor.cli import main

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
instruction_file: brain/agents/test-agent.md
"""

SCHEMA = {
    "required": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "string"},
        "department": {"type": "string", "enum": ["custom"]},
    }
}


def _workspace(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "brain" / "agents").mkdir(parents=True)
    (root / "templates" / "agents").mkdir(parents=True)
    (root / "docs" / "agents" / "schema.yaml").write_text(yaml.dump(SCHEMA))
    (root / "templates" / "agents" / "_defaults.yaml").write_text(yaml.dump({}))
    return root


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace the CLI resolves from the environment."""
    root = _workspace(tmp_path / "workspace")
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    from robothor.settings import reset_settings

    reset_settings()
    yield root
    reset_settings()


@pytest.fixture
def installed(workspace):
    (workspace / "docs" / "agents" / "test-agent.yaml").write_text(MANIFEST)
    (workspace / "brain" / "agents" / "test-agent.md").write_text("# Test Agent\n")
    return workspace


class TestParserSurface:
    @pytest.mark.parametrize(
        "argv",
        [
            ["agent", "export", "--help"],
            ["agent", "install", "--help"],
        ],
    )
    def test_help_exits_zero(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()


class TestExport:
    def test_export_writes_an_archive_and_exits_zero(self, installed, tmp_path, capsys):
        out = tmp_path / "test-agent.tar.gz"
        assert main(["agent", "export", "test-agent", "--out", str(out)]) == 0
        assert out.is_file()
        with tarfile.open(out, "r:gz") as archive:
            assert "test-agent/bundle.yaml" in archive.getnames()
        assert "test-agent" in capsys.readouterr().out

    def test_a_credential_literal_exits_two_and_writes_nothing(self, installed, tmp_path, capsys):
        (installed / "brain" / "agents" / "test-agent.md").write_text(
            "# Test Agent\n\nAPI_KEY=sk-or-v1-abcdefghijklmnop\n"
        )
        out = tmp_path / "test-agent.tar.gz"

        assert main(["agent", "export", "test-agent", "--out", str(out)]) == 2

        assert not out.exists()
        captured = capsys.readouterr()
        assert "sk-or-v1-abcdefghijklmnop" not in captured.out + captured.err

    def test_an_unknown_agent_exits_one(self, workspace, tmp_path):
        assert main(["agent", "export", "nobody", "--out", str(tmp_path / "x.tar.gz")]) == 1


class TestInstall:
    def _exported(self, installed, tmp_path):
        out = tmp_path / "test-agent.tar.gz"
        assert main(["agent", "export", "test-agent", "--out", str(out)]) == 0
        return out

    def test_without_yes_the_plan_is_printed_and_nothing_is_written(
        self, installed, tmp_path, capsys, monkeypatch
    ):
        archive = self._exported(installed, tmp_path)
        target = _workspace(tmp_path / "target")
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(target))

        assert main(["agent", "install", str(archive)]) == 0

        out = capsys.readouterr().out
        assert "docs/agents/test-agent.yaml" in out
        assert not (target / "docs" / "agents" / "test-agent.yaml").exists()

    def test_yes_installs(self, installed, tmp_path, monkeypatch):
        archive = self._exported(installed, tmp_path)
        target = _workspace(tmp_path / "target")
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(target))

        assert main(["agent", "install", str(archive), "--yes"]) == 0
        assert (target / "docs" / "agents" / "test-agent.yaml").is_file()

    def test_a_collision_exits_two(self, installed, tmp_path, monkeypatch):
        archive = self._exported(installed, tmp_path)
        target = _workspace(tmp_path / "target")
        (target / "docs" / "agents" / "test-agent.yaml").write_text(MANIFEST)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(target))

        assert main(["agent", "install", str(archive), "--yes"]) == 2

    def test_id_installs_alongside(self, installed, tmp_path, monkeypatch):
        archive = self._exported(installed, tmp_path)
        target = _workspace(tmp_path / "target")
        (target / "docs" / "agents" / "test-agent.yaml").write_text(MANIFEST)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(target))

        assert main(["agent", "install", str(archive), "--yes", "--id", "triage-bot"]) == 0
        assert (target / "docs" / "agents" / "triage-bot.yaml").is_file()

    def test_a_wheel_exits_two_naming_the_plugin_verb(self, workspace, tmp_path, capsys):
        wheel = tmp_path / "genus_billing-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"PK\x03\x04")
        assert main(["agent", "install", str(wheel)]) == 2
        assert "genus plugin install" in capsys.readouterr().out

    def test_a_url_without_a_sha256_exits_two(self, workspace, capsys):
        assert main(["agent", "install", "https://example.invalid/a.tar.gz"]) == 2
        assert "--sha256" in capsys.readouterr().out
