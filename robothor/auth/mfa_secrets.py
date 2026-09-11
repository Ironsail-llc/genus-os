"""Encryption at rest for the TOTP secrets in ``user_accounts.mfa_secret_enc``.

A second factor whose shared secret sits in plaintext in a table is a second
factor in name only: any SQL-read bug, any copied backup, any operator with a
psql prompt reconstitutes every user's authenticator. So the secret is sealed
with AES-256-GCM before it is written and only ever opened in process memory.

**Key choice.** ``robothor.vault.crypto`` also does AES-256-GCM, but its master
key is a file at ``$ROBOTHOR_WORKSPACE/.vault-key`` — an artifact of a
workspace install that a containerized bridge under Helm does not necessarily
have, and whose absence raises ``FileNotFoundError``. Local login must work in
exactly that deployment. The key here is therefore derived with HKDF-SHA256
(info ``b"genus-mfa-secret"``) from the auth signing key that
``robothor.auth.tokens.signing_key()`` already resolves — env first, then the
vault, generating on first boot. That key is present wherever local login can
function at all, because it is what signs the tokens local login mints.

Deriving from the signing key adds no new exposure: anyone holding it can
already mint an owner access token and does not need anyone's TOTP seed. What
it does add is a rotation hazard — rotating ``GENUS_AUTH_SIGNING_KEY`` makes
existing ``mfa_secret_enc`` values undecryptable. That fails CLOSED (the
affected user cannot pass the challenge, never bypasses it); recovery is
``genus user mfa-reset <email>`` followed by re-enrollment.
"""

from __future__ import annotations

import base64
import logging
import secrets

logger = logging.getLogger(__name__)

_INFO = b"genus-mfa-secret"
_NONCE_BYTES = 12
_KEY_BYTES = 32

_cached_key: bytes | None = None


def reset_key_cache() -> None:
    """Drop the derived-key cache (tests; and after a deliberate rotation)."""
    global _cached_key
    _cached_key = None


def _derived_key() -> bytes:
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from robothor.auth.tokens import signing_key

    _cached_key = HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=_INFO).derive(
        signing_key().encode("utf-8")
    )
    return _cached_key


def _aad(user_id: str) -> bytes:
    """Associated data binding a ciphertext to one account row.

    Without it every row is sealed under the same key, so a blob copied from
    the attacker's own row into the owner's row decrypts perfectly and the
    attacker's authenticator now opens the owner's account. AES-GCM
    authenticates associated data without encrypting it, so the bind costs
    nothing and a transplanted blob fails the tag check — i.e. decrypts to
    None, which every caller treats as "factor unusable, refuse".
    """
    return b"genus-mfa-user:" + (user_id or "").encode("utf-8")


def encrypt_secret(plaintext: str, *, user_id: str) -> str:
    """Seal a base32 TOTP secret for ONE account.

    Returns base64(nonce || ciphertext || tag), bound to ``user_id``.
    """
    if not plaintext:
        raise ValueError("refusing to encrypt an empty TOTP secret")
    if not user_id:
        raise ValueError("a TOTP secret must be bound to an account id")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(_derived_key()).encrypt(nonce, plaintext.encode("utf-8"), _aad(user_id))
    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_secret(blob: str | None, *, user_id: str) -> str | None:
    """Open a sealed secret for ``user_id``, or ``None`` if it cannot be opened.

    Never raises and never logs the value: a caller that gets ``None`` must
    treat the factor as unusable and refuse the sign-in, which is what every
    caller in ``local_login`` does. A blob sealed for a DIFFERENT account is
    one of the cases that returns ``None``.
    """
    if not blob or not blob.strip() or not user_id:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.b64decode(blob.strip(), validate=True)
    except Exception:
        return None
    if len(raw) <= _NONCE_BYTES:
        return None
    try:
        opened = AESGCM(_derived_key()).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(user_id)
        )
    except Exception:
        # Wrong key (signing-key rotation), tampered ciphertext, or a blob
        # transplanted from another account. Identifiers only — the blob
        # itself never reaches a log record.
        logger.warning("mfa_secrets: stored TOTP secret could not be decrypted")
        return None
    try:
        return opened.decode("utf-8")
    except UnicodeDecodeError:
        return None
