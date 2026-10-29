"""One vault master key and one fingerprint salt for the bridge and every engine cell.

Both files used to be found only under ``<workspace>/`` (``.vault-key`` and
``.fingerprint-salt``), so a deployment running the bridge and several engine
cells in separate pods could not give them ONE shared key: each would read, or
generate, its own, and a secret written by one would not decrypt in another.

``GENUS_VAULT_KEY_FILE`` and ``GENUS_FINGERPRINT_SALT_FILE`` name the file
outright. Under an override nothing is ever generated: the file is typically a
read-only Secret mount, and a key generated beside it would silently orphan
every stored secret. Unset, everything behaves exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

KEY = bytes(range(32))
SALT = bytes(range(100, 132))


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from robothor.secrets import fingerprint
    from robothor.vault import crypto

    for name in (
        "GENUS_VAULT_KEY_FILE",
        "ROBOTHOR_VAULT_KEY_FILE",
        "GENUS_FINGERPRINT_SALT_FILE",
        "ROBOTHOR_FINGERPRINT_SALT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "workspace"))
    crypto.reset_key_cache()
    fingerprint.reset_fingerprint_key()
    yield
    crypto.reset_key_cache()
    fingerprint.reset_fingerprint_key()


def _mounted(tmp_path: Path, name: str, content: bytes) -> Path:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    path = mount / name
    path.write_bytes(content)
    return path


# ── the vault master key ─────────────────────────────────────────────


@pytest.mark.parametrize("env", ["GENUS_VAULT_KEY_FILE", "ROBOTHOR_VAULT_KEY_FILE"])
def test_get_master_key_reads_the_override(tmp_path, monkeypatch, env) -> None:
    from robothor.vault.crypto import get_master_key

    monkeypatch.setenv(env, str(_mounted(tmp_path, "key", KEY)))
    assert get_master_key() == KEY


def test_the_override_wins_over_an_explicit_workspace(tmp_path, monkeypatch) -> None:
    from robothor.vault.crypto import get_master_key

    workspace = tmp_path / "other"
    workspace.mkdir()
    (workspace / ".vault-key").write_bytes(b"\x01" * 32)
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(_mounted(tmp_path, "key", KEY)))
    assert get_master_key(workspace) == KEY


def test_a_missing_override_raises_naming_the_variable(tmp_path, monkeypatch) -> None:
    from robothor.vault.crypto import get_master_key

    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(tmp_path / "nowhere" / "key"))
    with pytest.raises(FileNotFoundError, match="GENUS_VAULT_KEY_FILE"):
        get_master_key()
    assert not (tmp_path / "nowhere").exists()


def test_a_wrong_length_override_raises_naming_the_variable(tmp_path, monkeypatch) -> None:
    from robothor.vault.crypto import get_master_key

    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(_mounted(tmp_path, "key", b"short")))
    with pytest.raises(ValueError, match="GENUS_VAULT_KEY_FILE"):
        get_master_key()


def test_the_override_is_raw_bytes_not_base64(tmp_path, monkeypatch) -> None:
    """The chart mounts the RAW 32 bytes (the secret sync decodes base64).
    A file still holding the base64 text is 44 bytes and must be refused, not
    decoded: two readers that disagree about the encoding hold two keys."""
    import base64

    from robothor.vault.crypto import get_master_key

    encoded = base64.b64encode(KEY)
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(_mounted(tmp_path, "key", encoded)))
    with pytest.raises(ValueError, match="GENUS_VAULT_KEY_FILE"):
        get_master_key()


def test_the_override_is_read_byte_for_byte(tmp_path, monkeypatch) -> None:
    """No stripping: a raw key may legitimately end in whitespace bytes."""
    from robothor.vault.crypto import get_master_key

    raw = bytes(range(31)) + b"\n"
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(_mounted(tmp_path, "key", raw)))
    assert get_master_key() == raw


def test_init_under_an_override_validates_and_never_generates(tmp_path, monkeypatch) -> None:
    from robothor.vault.crypto import init_master_key

    path = _mounted(tmp_path, "key", KEY)
    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(path))
    workspace = tmp_path / "workspace"
    assert init_master_key(workspace) == path
    assert path.read_bytes() == KEY
    assert not (workspace / ".vault-key").exists()


def test_init_under_a_missing_override_raises_and_generates_nothing(tmp_path, monkeypatch) -> None:
    from robothor.vault.crypto import init_master_key

    monkeypatch.setenv("GENUS_VAULT_KEY_FILE", str(tmp_path / "mount" / "key"))
    workspace = tmp_path / "workspace"
    with pytest.raises(FileNotFoundError, match="GENUS_VAULT_KEY_FILE"):
        init_master_key(workspace)
    assert not (tmp_path / "mount" / "key").exists()
    assert not (workspace / ".vault-key").exists()


def test_unset_keeps_the_workspace_key(tmp_path) -> None:
    from robothor.vault.crypto import get_master_key, init_master_key

    workspace = tmp_path / "workspace"
    path = init_master_key(workspace)
    assert path == workspace / ".vault-key"
    assert get_master_key() == path.read_bytes()


# ── the fingerprint salt ─────────────────────────────────────────────


def test_two_workspaces_sharing_the_salt_override_agree(tmp_path, monkeypatch) -> None:
    from robothor.secrets import fingerprint as module

    salt = _mounted(tmp_path, "fingerprint-salt", SALT)
    monkeypatch.setenv("GENUS_FINGERPRINT_SALT_FILE", str(salt))

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "cell-a"))
    module.reset_fingerprint_key()
    first = module.fingerprint("a-credential")

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "cell-b"))
    module.reset_fingerprint_key()
    assert module.fingerprint("a-credential") == first
    assert not (tmp_path / "cell-a" / ".fingerprint-salt").exists()
    assert not (tmp_path / "cell-b" / ".fingerprint-salt").exists()


def test_the_salt_alias_is_honoured(tmp_path, monkeypatch) -> None:
    from robothor.secrets import fingerprint as module

    monkeypatch.setenv("ROBOTHOR_FINGERPRINT_SALT_FILE", str(_mounted(tmp_path, "salt", SALT)))
    with_override = module.fingerprint("a-credential")
    monkeypatch.delenv("ROBOTHOR_FINGERPRINT_SALT_FILE")
    module.reset_fingerprint_key()
    assert module.fingerprint("a-credential") != with_override


def test_a_missing_salt_override_is_never_generated(tmp_path, monkeypatch) -> None:
    """A fingerprint must not take a status page down, so a missing override
    degrades (loudly) rather than raising — but it never writes a salt."""
    from robothor.secrets import fingerprint as module

    target = tmp_path / "mount" / "fingerprint-salt"
    monkeypatch.setenv("GENUS_FINGERPRINT_SALT_FILE", str(target))
    module.fingerprint("a-credential")
    assert not target.exists()
    assert not (tmp_path / "workspace" / ".fingerprint-salt").exists()


def test_unset_keeps_the_workspace_salt(tmp_path) -> None:
    from robothor.secrets import fingerprint as module

    module.fingerprint("a-credential")
    assert (tmp_path / "workspace" / ".fingerprint-salt").exists()


# ── declared like every other setting ────────────────────────────────


@pytest.mark.parametrize(
    ("name", "alias"),
    [
        ("GENUS_VAULT_KEY_FILE", "ROBOTHOR_VAULT_KEY_FILE"),
        ("GENUS_FINGERPRINT_SALT_FILE", "ROBOTHOR_FINGERPRINT_SALT_FILE"),
    ],
)
def test_the_settings_are_declared(name: str, alias: str) -> None:
    from robothor.settings.aliases import DEPRECATED_ALIASES
    from robothor.settings.registry import declared_env_names

    assert name in declared_env_names()
    assert DEPRECATED_ALIASES.get(alias) == name
