"""Redis lease primitive (Wave-2, W2-1 — HA foundation).

A mutual-exclusion lease with owner tokens + TTL. This is the substrate for
making the engine HA: Redis-backed dedup, fleet-pool admission, and a
leader-elected scheduler all build on ``acquire``/``renew``/``release``.

A crashed holder's lease auto-expires (PX TTL), so a survivor reclaims it on the
next ``acquire`` — that is the stale-claim reclaim. ``renew``/``release`` use a
compare-and-act Lua script so a process can only affect a lease it still owns
(never stomping a lease that TTL'd out and was re-acquired by someone else).

These are synchronous (redis-py sync client, mirroring messaging.py). Async
callers should wrap in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Stable per-process identity: PID + a boot token generated once at import, so a
# recycled PID after a restart never collides with a prior process's owner token.
_BOOT_TOKEN = uuid.uuid4().hex[:12]


def default_owner() -> str:
    """Owner token identifying THIS process for the lifetime of the lease."""
    return f"{os.getpid()}:{_BOOT_TOKEN}"


def _client(redis_client: Any = None) -> Any:
    if redis_client is not None:
        return redis_client
    import redis

    from robothor.config import get_config

    cfg = get_config()
    return redis.Redis(
        host=cfg.redis.host,
        port=cfg.redis.port,
        db=cfg.redis.db,
        password=cfg.redis.password or None,
    )


# Only act if we still own the key (compare-and-act).
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_RENEW_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"


def acquire(
    key: str, ttl_ms: int, *, owner: str | None = None, redis_client: Any = None
) -> str | None:
    """Try to acquire the lease. Returns the owner token on success, else None.

    Uses ``SET key owner NX PX ttl_ms`` — atomic; only one caller wins.
    """
    owner = owner or default_owner()
    r = _client(redis_client)
    ok = r.set(key, owner, nx=True, px=ttl_ms)
    return owner if ok else None


def renew(key: str, owner: str, ttl_ms: int, *, redis_client: Any = None) -> bool:
    """Extend the lease TTL — only if ``owner`` still holds it."""
    r = _client(redis_client)
    return bool(r.eval(_RENEW_LUA, 1, key, owner, ttl_ms))


def release(key: str, owner: str, *, redis_client: Any = None) -> bool:
    """Release the lease — only if ``owner`` still holds it. Idempotent."""
    r = _client(redis_client)
    return bool(r.eval(_RELEASE_LUA, 1, key, owner))


def current_owner(key: str, *, redis_client: Any = None) -> str | None:
    """Who currently holds the lease (or None)."""
    r = _client(redis_client)
    val = r.get(key)
    if val is None:
        return None
    return val.decode() if isinstance(val, bytes) else str(val)
