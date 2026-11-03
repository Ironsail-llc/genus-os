"""
AES-256-GCM encryption for vault secrets.

Master key is a 32-byte random key stored at $WORKSPACE/.vault-key (chmod 600),
or at the file ``GENUS_VAULT_KEY_FILE`` names. Each secret gets a unique 12-byte
nonce prepended to the ciphertext.

The override exists so the bridge and every engine cell of one deployment can
share ONE key, mounted read-only from a Secret. Under it nothing is generated:
a key written beside a missing mount would encrypt new secrets under a key no
other process has, and orphan them the moment the mount appeared.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

_cached_key: bytes | None = None

KEY_FILE_ENV = "GENUS_VAULT_KEY_FILE"
_KEY_FILE_ALIAS = "ROBOTHOR_VAULT_KEY_FILE"


def key_file_override() -> Path | None:
    """The key file ``GENUS_VAULT_KEY_FILE`` names, or None when it is unset."""
    from robothor.settings.env import process_env_get

    value = (process_env_get(KEY_FILE_ENV, "") or "").strip()
    if not value:
        value = (process_env_get(_KEY_FILE_ALIAS, "") or "").strip()
        if value:
            from robothor.settings.aliases import warn_deprecated_alias

            warn_deprecated_alias(_KEY_FILE_ALIAS)
    return Path(value) if value else None


def _read_override(path: Path) -> bytes:
    """The overriding key, validated. Raises naming the variable; never writes."""
    try:
        key = path.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Vault master key not found at {path} ({KEY_FILE_ENV}). It is never "
            "generated under that setting: mount the deployment's shared key there, "
            f"or unset {KEY_FILE_ENV} to use the workspace key."
        ) from exc
    except OSError as exc:
        raise PermissionError(
            f"Vault master key at {path} ({KEY_FILE_ENV}) cannot be read: {type(exc).__name__}."
        ) from exc
    if len(key) != 32:
        raise ValueError(
            f"Vault master key at {path} ({KEY_FILE_ENV}) must be 32 bytes, got {len(key)}"
        )
    return key


def init_master_key(workspace: Path | str) -> Path:
    """Generate a new master key file. Returns the path. Idempotent — skips if exists.

    Under ``GENUS_VAULT_KEY_FILE`` it only validates that file and returns its
    path: first-boot generation is for the workspace key alone.
    """
    override = key_file_override()
    if override is not None:
        _read_override(override)
        return override
    key_path = Path(workspace) / ".vault-key"
    if key_path.exists():
        return key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(secrets.token_bytes(32))
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    return key_path


def get_master_key(workspace: Path | str | None = None) -> bytes:
    """Load the master key from disk (cached after first read)."""
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    override = key_file_override()
    if override is not None:
        _cached_key = _read_override(override)
        return _cached_key

    if workspace is None:
        workspace = os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor")
    key_path = Path(workspace) / ".vault-key"
    if not key_path.exists():
        raise FileNotFoundError(
            f"Vault master key not found at {key_path}. "
            "Run 'robothor vault init' or 'robothor init' to generate one."
        )
    key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError(f"Vault master key must be 32 bytes, got {len(key)}")
    _cached_key = key
    return _cached_key


def reset_key_cache() -> None:
    """Clear the cached master key (for testing)."""
    global _cached_key
    _cached_key = None


def encrypt(plaintext: str, master_key: bytes) -> bytes:
    """Encrypt plaintext with AES-256-GCM. Returns nonce (12 bytes) + ciphertext + tag (16 bytes)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(master_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt(data: bytes, master_key: bytes) -> str:
    """Decrypt nonce + ciphertext + tag back to plaintext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(data) < 28:  # 12 nonce + 16 tag minimum
        raise ValueError("Encrypted data too short")
    nonce = data[:12]
    ciphertext = data[12:]
    aesgcm = AESGCM(master_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
