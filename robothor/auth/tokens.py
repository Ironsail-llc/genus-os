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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

import jwt

ALGORITHM = "HS256"
ACCESS_TTL_SECONDS = 15 * 60  # 15 minutes
SERVICE_TTL_SECONDS = 5 * 60  # service credentials are deliberately short-lived
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
_ISSUER = "genus-os"
DEFAULT_AUDIENCE = "genus-bridge"

_HUMAN_ROLES = frozenset({"owner", "admin", "member", "user", "viewer", "auditor"})
_TOKEN_TYPES = frozenset({"user", "service"})

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
        if len(env.encode("utf-8")) < 32:
            raise TokenError("GENUS_AUTH_SIGNING_KEY must contain at least 32 bytes")
        _signing_key_cache = env
        return env

    # Lazy import: keep token encode/decode usable without the vault when a key
    # is provided via env (tests, edge cases).
    from robothor import vault

    key = vault.get(_VAULT_KEY)
    if not key:
        key = secrets.token_urlsafe(48)
        vault.set(_VAULT_KEY, key, category="auth")
    if len(key.encode("utf-8")) < 32:
        raise TokenError("resolved signing key must contain at least 32 bytes")
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
    audience: str = DEFAULT_AUDIENCE,
    scopes: Iterable[str] | None = None,
    agent_id: str | None = None,
) -> str:
    """Mint a signed access JWT carrying a complete verified identity.

    ``typ="service"`` binds the token to an ``agent_id`` (defaulting to
    ``user_id`` for source compatibility).  Callers may select another
    audience for engine/orchestrator tokens, but verifiers must request that
    same audience explicitly.
    """
    if typ not in _TOKEN_TYPES:
        raise ValueError(f"unsupported token type: {typ}")
    if not user_id or not tenant_id or not audience:
        raise ValueError("user_id, tenant_id, and audience are required")

    if typ == "service":
        agent_id = agent_id or user_id
        role = role or "service"
    elif agent_id is not None:
        raise ValueError("user tokens cannot carry agent_id")

    normalized_scopes = _normalize_scopes(scopes or _default_scopes(typ, role))
    if not normalized_scopes:
        raise ValueError("at least one scope is required")

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "tid": tenant_id,
        "tenant": tenant_id,
        "role": role,
        "typ": typ,
        "type": typ,
        "aud": audience,
        "scope": " ".join(normalized_scopes),
        "iss": _ISSUER,
        "jti": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if agent_id is not None:
        claims["agent_id"] = agent_id
    return str(jwt.encode(claims, signing_key(), algorithm=ALGORITHM))


def issue_service_token(
    service_id: str,
    tenant_id: str,
    *,
    agent_id: str | None = None,
    audience: str = DEFAULT_AUDIENCE,
    scopes: Iterable[str] = ("bridge:read", "bridge:write"),
    ttl_seconds: int = SERVICE_TTL_SECONDS,
) -> str:
    """Mint a short-lived, audience-bound token for an internal service/agent."""
    return issue_access_token(
        service_id,
        tenant_id,
        "service",
        ttl_seconds=ttl_seconds,
        typ="service",
        audience=audience,
        scopes=scopes,
        agent_id=agent_id or service_id,
    )


def decode_token(token: str, *, expected_audience: str = DEFAULT_AUDIENCE) -> dict[str, Any]:
    """Verify signature + expiry and return claims. Raises ``TokenError``."""
    if not token:
        raise TokenError("empty token")
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=[ALGORITHM],
            issuer=_ISSUER,
            audience=expected_audience,
            options={
                "require": [
                    "aud",
                    "exp",
                    "iat",
                    "iss",
                    "jti",
                    "role",
                    "scope",
                    "sub",
                    "tid",
                    "typ",
                ]
            },
        )
        return dict(claims)
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e


def _default_scopes(typ: str, role: str) -> tuple[str, ...]:
    if typ == "service":
        return ("bridge:read", "bridge:write")
    by_role = {
        # Human Bridge sessions are also the dashboard's delegated identity at
        # the Engine BFF boundary.  The Engine still enforces the role against
        # role_permissions for every tool call; these scopes only decide which
        # coarse HTTP surfaces the signed user may enter.
        "viewer": ("bridge:read", "engine:chat", "engine:read"),
        "auditor": ("audit:read", "bridge:read", "engine:read"),
        "member": (
            "bridge:read",
            "bridge:write",
            "engine:chat",
            "engine:read",
            "engine:write",
        ),
        "user": (
            "bridge:read",
            "bridge:write",
            "engine:chat",
            "engine:read",
            "engine:write",
        ),
        "admin": ("audit:read", "bridge:*", "engine:*", "tenant:admin"),
        "owner": ("audit:read", "bridge:*", "engine:*", "tenant:admin"),
    }
    if role not in _HUMAN_ROLES:
        raise ValueError(f"unsupported user role: {role}")
    return by_role[role]


def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    normalized = {str(scope).strip() for scope in scopes if str(scope).strip()}
    if any(" " in scope for scope in normalized):
        raise ValueError("individual scopes cannot contain whitespace")
    return tuple(sorted(normalized))


def new_refresh_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hash)``. Store the hash; hand the raw to the client."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    """SHA-256 of the opaque refresh token (the only form persisted)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
