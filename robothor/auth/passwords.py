"""Password hashing for break-glass / non-SSO local accounts.

Primary auth is SSO (OIDC/SAML), so most accounts have ``password_hash IS NULL``.
This module exists for the admin break-glass path (Phase D) and to keep a single,
well-reviewed hashing choice. Uses argon2id (argon2-cffi) with library defaults.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2id hash string for ``password``."""
    if not password:
        raise ValueError("password must be non-empty")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time-ish verify. False (never raises) on mismatch / no hash set."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether the stored hash should be upgraded to current parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False
