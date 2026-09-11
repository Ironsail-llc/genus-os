"""TOTP secrets are encrypted at rest, and fail CLOSED when they cannot be read."""

from __future__ import annotations

import base64

import pytest

from robothor.auth import mfa_secrets

KEY_A = "test-signing-key-at-least-32-bytes-long-xyz"
KEY_B = "a-completely-different-signing-key-32-bytes"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY_A)
    tokens.reset_signing_key_cache()
    mfa_secrets.reset_key_cache()
    yield
    tokens.reset_signing_key_cache()
    mfa_secrets.reset_key_cache()


def test_round_trip() -> None:
    blob = mfa_secrets.encrypt_secret("JBSWY3DPEHPK3PXP")
    assert mfa_secrets.decrypt_secret(blob) == "JBSWY3DPEHPK3PXP"


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    blob = mfa_secrets.encrypt_secret("JBSWY3DPEHPK3PXP")
    assert "JBSWY3DPEHPK3PXP" not in blob
    assert b"JBSWY3DPEHPK3PXP" not in base64.b64decode(blob)


def test_each_encryption_uses_a_fresh_nonce() -> None:
    assert mfa_secrets.encrypt_secret("SAME") != mfa_secrets.encrypt_secret("SAME")


def test_tampered_ciphertext_does_not_decrypt() -> None:
    raw = bytearray(base64.b64decode(mfa_secrets.encrypt_secret("JBSWY3DPEHPK3PXP")))
    raw[-1] ^= 0xFF
    assert mfa_secrets.decrypt_secret(base64.b64encode(bytes(raw)).decode()) is None


def test_a_different_signing_key_cannot_decrypt(monkeypatch) -> None:
    blob = mfa_secrets.encrypt_secret("JBSWY3DPEHPK3PXP")
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY_B)
    tokens.reset_signing_key_cache()
    mfa_secrets.reset_key_cache()
    assert mfa_secrets.decrypt_secret(blob) is None


def test_garbage_and_absent_values_return_none_rather_than_raising() -> None:
    for bad in (None, "", "   ", "not-base64!!", base64.b64encode(b"short").decode()):
        assert mfa_secrets.decrypt_secret(bad) is None
