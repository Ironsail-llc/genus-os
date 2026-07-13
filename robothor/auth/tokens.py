"""Bridge-issued session tokens.

Two token types:
  - **access**: a short-TTL stateless JWT (HS256) carrying the verified
    ``{sub=user_id, tid=tenant_id, role, typ:"user"}``. Verified on every bridge
    call with no DB hit — this is the hot path.
  - **refresh**: an opaque random string. Only its SHA-256 hash is stored
    (``user_sessions``), so logout / revoke / "log out everywhere" are real.

Signing key resolution (cached): ``GENUS_AUTH_SIGNING_KEY`` env → vault
``auth/jwt_signing_key`` → generate-and-store-in-vault on first boot. The key is
HS256 (symmetric): the bridge both signs and verifies; the dashboard server,
which mints tokens after SSO, shares the key via the same secret channel.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Any

import jwt

ALGORITHM = "HS256"
ACCESS_TTL_SECONDS = 15 * 60  # 15 minutes
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
_ISSUER = "genus-os"

_VAULT_KEY = "auth/jwt_signing_key"
_signing_key_cache: str | None = None


class TokenError(Exception):
    """Token is missing, malformed, expired, or fails signature verification."""


def signing_key() -> str:
    """Resolve the HS256 signing key (cached). Generates + stores on first boot."""
    global _signing_key_cache
    if _signing_key_cache:
        return _signing_key_cache

    env = os.environ.get("GENUS_AUTH_SIGNING_KEY")
    if env:
        _signing_key_cache = env
        return env

    # Lazy import: keep token encode/decode usable without the vault when a key
    # is provided via env (tests, edge cases).
    from robothor import vault

    key = vault.get(_VAULT_KEY)
    if not key:
        key = secrets.token_urlsafe(48)
        vault.set(_VAULT_KEY, key, category="auth")
    _signing_key_cache = key
    return key


def reset_signing_key_cache() -> None:
    """Test hook — forget the cached key so the next call re-resolves."""
    global _signing_key_cache
    _signing_key_cache = None


def issue_access_token(
    user_id: str,
    tenant_id: str,
    role: str,
    *,
    ttl_seconds: int = ACCESS_TTL_SECONDS,
    typ: str = "user",
) -> str:
    """Mint a signed access JWT carrying the verified identity + role."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "typ": typ,
        "iss": _ISSUER,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return str(jwt.encode(claims, signing_key(), algorithm=ALGORITHM))


def decode_token(token: str) -> dict[str, Any]:
    """Verify signature + expiry and return claims. Raises ``TokenError``."""
    if not token:
        raise TokenError("empty token")
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=[ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
        return dict(claims)
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e


def new_refresh_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hash)``. Store the hash; hand the raw to the client."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    """SHA-256 of the opaque refresh token (the only form persisted)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
