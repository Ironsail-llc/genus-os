"""A glob or a brace expansion must not walk a secret past the exec guard.

The guard reads argument TEXT, and the shell expands it afterwards: ``.en?``
is not a secrets name, and ``.env`` is what ``cp`` receives. Every command
below printed a secret on a real stack (and on ``main``) before this rule.

A token containing ``* ? [ ] { }`` is therefore judged by what it COULD name:
its braces are expanded, each alternative without a wildcard is checked as a
path, and a wildcard is refused when it could match a secrets name, or when it
does match a refused file in the command's directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from robothor.engine.secret_paths import exec_reads_secret

if TYPE_CHECKING:
    from pathlib import Path

ENVS = (
    "GENUS_VAULT_KEY_FILE",
    "ROBOTHOR_VAULT_KEY_FILE",
    "GENUS_FINGERPRINT_SALT_FILE",
    "ROBOTHOR_FINGERPRINT_SALT_FILE",
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".env").write_text("TOKEN=s3cr3t\n")
    (root / "notes.txt").write_text("hello\n")
    (root / "docs" / "guide.md").write_text("# guide\n")
    (root / "src" / "app.py").write_text("print(1)\n")
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(root))
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize(
    "command",
    [
        "cp .en? leak.txt && cat leak.txt",
        "cp {.env,leak.txt} && cat leak.txt",
        "tar cf - .en? | tar xOf -",
        "cat .env*",
        "cat *.env",
        "xxd < .en?",
    ],
)
def test_a_wildcard_that_can_name_a_secret_is_refused(workspace, command) -> None:
    assert exec_reads_secret(command), command


@pytest.mark.parametrize(
    "command",
    ["cat instance-ke?", "cp instance-k* x", "base64 instance-{key,x}"],
)
def test_a_wildcard_that_can_name_a_relative_override_is_refused(
    workspace, monkeypatch, command
) -> None:
    (workspace / "instance-key").write_bytes(b"\x00" * 32)
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", "instance-key")
    assert exec_reads_secret(command), command


def test_a_wildcard_matching_a_refused_file_on_disk_is_refused(workspace) -> None:
    """No literal prefix to go on: the pattern is expanded where it will run."""
    (workspace / "conf").mkdir()
    (workspace / "conf" / "prod.pem").write_text("-----BEGIN")
    assert exec_reads_secret("cat conf/*")


@pytest.mark.parametrize(
    "command",
    [
        "ls *.py",
        "cat docs/*.md",
        "cp src/*.py build/",
        "cat notes.tx?",
        "wc -l {notes,README}.txt",
        "rm -f build/*.o",
        "echo .en?",
    ],
)
def test_ordinary_globs_still_run(workspace, command) -> None:
    assert exec_reads_secret(command) is None, command


@pytest.mark.parametrize("command", ["cat c*.txt", "cp c*.py build/", "ls s*", "head s*.md"])
def test_a_wildcard_that_merely_starts_like_a_secret_name_runs(workspace, command) -> None:
    """`c` starts `credentials` and `s` starts `secrets`; neither pattern can
    MATCH a secrets name, and nothing here expands to one."""
    assert exec_reads_secret(command) is None, command


def test_a_wildcard_that_expands_to_a_secret_file_is_refused(workspace) -> None:
    (workspace / "credentials.json").write_text("{}")
    assert exec_reads_secret("cat c*")
