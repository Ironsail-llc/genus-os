"""Tests for vault crypto operations."""

import secrets
import stat
from pathlib import Path

import pytest

from robothor.vault.crypto import (
    decrypt,
    encrypt,
    get_master_key,
    init_master_key,
    reset_key_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the cached master key before each test."""
    reset_key_cache()
    yield
    reset_key_cache()


class TestEncryptDecrypt:
    def test_roundtrip(self):
        key = secrets.token_bytes(32)
        plaintext = "my-secret-api-key-123"
        encrypted = encrypt(plaintext, key)
        assert decrypt(encrypted, key) == plaintext

    def test_different_nonces(self):
        key = secrets.token_bytes(32)
        a = encrypt("same", key)
        b = encrypt("same", key)
        assert a != b  # Different nonces

    def test_wrong_key_fails(self):
        key1 = secrets.token_bytes(32)
        key2 = secrets.token_bytes(32)
        encrypted = encrypt("secret", key1)
        with pytest.raises(Exception):
            decrypt(encrypted, key2)

    def test_empty_string(self):
        key = secrets.token_bytes(32)
        encrypted = encrypt("", key)
        assert decrypt(encrypted, key) == ""

    def test_unicode(self):
        key = secrets.token_bytes(32)
        plaintext = "sekrit: \U0001f511 emoji-key"
        assert decrypt(encrypt(plaintext, key), key) == plaintext

    def test_truncated_data_fails(self):
        key = secrets.token_bytes(32)
        with pytest.raises(ValueError, match="too short"):
            decrypt(b"short", key)


class TestMasterKey:
    def test_init_creates_key(self, tmp_path: Path):
        key_path = init_master_key(tmp_path)
        assert key_path.exists()
        assert key_path.read_bytes().__len__() == 32
        mode = key_path.stat().st_mode
        assert mode & stat.S_IRGRP == 0  # No group read
        assert mode & stat.S_IROTH == 0  # No other read

    def test_init_idempotent(self, tmp_path: Path):
        init_master_key(tmp_path)
        first_key = (tmp_path / ".vault-key").read_bytes()
        init_master_key(tmp_path)
        assert (tmp_path / ".vault-key").read_bytes() == first_key

    def test_get_master_key(self, tmp_path: Path):
        init_master_key(tmp_path)
        key = get_master_key(tmp_path)
        assert len(key) == 32

    def test_get_master_key_missing(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="(?i)vault master key"):
            get_master_key(tmp_path)

    def test_get_master_key_wrong_length(self, tmp_path: Path):
        (tmp_path / ".vault-key").write_bytes(b"tooshort")
        with pytest.raises(ValueError, match="32 bytes"):
            get_master_key(tmp_path)

    def test_caching(self, tmp_path: Path):
        init_master_key(tmp_path)
        k1 = get_master_key(tmp_path)
        k2 = get_master_key(tmp_path)
        assert k1 is k2  # Same object (cached)
