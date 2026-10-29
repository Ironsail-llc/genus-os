"""An agent cannot read the vault key or the fingerprint salt where they were moved.

``secret_paths`` refused both by BASENAME (``.vault-key``,
``.fingerprint-salt``). ``GENUS_VAULT_KEY_FILE`` and
``GENUS_FINGERPRINT_SALT_FILE`` move them to any path — the chart mounts them
at an innocuous name such as ``key`` — so a basename rule let ``read_file`` and
``exec cat`` print the master key, which decrypts every row the vault holds.

So the RESOLVED override paths are refused too (``realpath``, so a symlink or
a ``..`` does not dodge it), and, defensively, every file in their directory.
The settings are read when the check runs, not at import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from robothor.engine.secret_paths import exec_reads_secret, is_secret_path

if TYPE_CHECKING:
    from pathlib import Path

ENVS = (
    "GENUS_VAULT_KEY_FILE",
    "ROBOTHOR_VAULT_KEY_FILE",
    "GENUS_FINGERPRINT_SALT_FILE",
    "ROBOTHOR_FINGERPRINT_SALT_FILE",
)


@pytest.fixture
def mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "workspace"))
    directory = tmp_path / "genus" / "vault"
    directory.mkdir(parents=True)
    (directory / "key").write_bytes(b"\x00" * 32)
    (directory / "fingerprint-salt").write_bytes(b"\x01" * 32)
    return directory


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ("GENUS_VAULT_KEY_FILE", "key"),
        ("ROBOTHOR_VAULT_KEY_FILE", "key"),
        ("GENUS_FINGERPRINT_SALT_FILE", "fingerprint-salt"),
        ("ROBOTHOR_FINGERPRINT_SALT_FILE", "fingerprint-salt"),
    ],
)
def test_the_override_file_is_a_secret_path(mount, monkeypatch, env, name) -> None:
    monkeypatch.setenv(env, str(mount / name))
    assert is_secret_path(mount / name)


def test_read_at_check_time_not_import_time(mount, monkeypatch) -> None:
    assert not is_secret_path(mount / "key")
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    assert is_secret_path(mount / "key")


def test_a_sibling_in_the_mount_directory_is_refused(mount, monkeypatch) -> None:
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    assert is_secret_path(mount / "anything-else")


def test_a_symlink_or_dotdot_does_not_dodge_it(mount, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    link = tmp_path / "innocent"
    link.symlink_to(mount / "key")
    assert is_secret_path(link)
    assert is_secret_path(mount / ".." / "vault" / "key")


def test_the_override_value_itself_may_be_a_symlink(mount, monkeypatch, tmp_path) -> None:
    """Secret mounts are symlinks into a timestamped directory."""
    link = tmp_path / "mounted-key"
    link.symlink_to(mount / "key")
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(link))
    assert is_secret_path(mount / "key")


def test_the_same_basename_elsewhere_is_allowed(mount, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    (elsewhere / "key").write_text("not a secret")
    assert not is_secret_path(elsewhere / "key")


def test_an_override_in_the_workspace_root_does_not_refuse_the_workspace(
    monkeypatch, tmp_path
) -> None:
    """The sibling rule is for a dedicated mount directory. Pointed at the
    workspace (or home, or /), it must not blind the agent to everything."""
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(workspace / "shared-key"))
    assert is_secret_path(workspace / "shared-key")
    assert not is_secret_path(workspace / "notes.txt")
    assert not is_secret_path(workspace / "README")


def test_exec_cat_of_the_override_is_refused(mount, monkeypatch) -> None:
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    assert exec_reads_secret(f"cat {mount / 'key'}")
    assert exec_reads_secret(f"head -c 32 {mount / 'key'} | base64")
    assert exec_reads_secret(f"xxd {mount / 'fingerprint-salt'}")


def test_exec_cat_elsewhere_is_allowed(mount, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    assert exec_reads_secret(f"cat {tmp_path / 'project' / 'key'}") is None


@pytest.mark.asyncio
async def test_read_file_refuses_the_override(mount, monkeypatch) -> None:
    from robothor.engine.tools.handlers import filesystem

    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(mount / "key"))
    ctx = MagicMock()
    ctx.workspace = str(mount.parent)
    out = await filesystem.HANDLERS["read_file"]({"path": str(mount / "key")}, ctx)
    assert "error" in out and "refused" in out["error"]
