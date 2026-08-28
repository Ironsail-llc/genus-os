"""DB-backed resolution for the 12 governed guardrail flags.

Resolution order, never violated: operator DB row -> os.environ -> None.

A DB that is *unreachable* falls through to env — it never returns None (which a
caller would read as off). Only an operator-written row overrides env; a bare
migration seed row is treated as "unset" so the env->DB cutover is a no-op.
"""

from __future__ import annotations

import os
import threading
import time

from robothor.db.connection import get_connection

GOVERNED_FLAGS: frozenset[str] = frozenset(
    {
        "ROBOTHOR_RBAC_MODE",
        "ROBOTHOR_INJECTION_SCAN_MODE",
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE",
        "ROBOTHOR_APPROVAL_MODE",
        "ROBOTHOR_SANDBOX_DEFAULT_MODE",
        "ROBOTHOR_ADMISSION_MODE",
        "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
        "ROBOTHOR_RIP_7_MODE",
        "ROBOTHOR_RIP_13_MODE",
        "ROBOTHOR_RIP_1_ENABLED",
        "ROBOTHOR_RIP_4_ENABLED",
        "ROBOTHOR_RIP_5_ENABLED",
        "ROBOTHOR_JUDGE_ENABLED",
    }
)

_MODE_VALUES: tuple[str, ...] = ("off", "observe", "alert", "enforce")
_RIP_13_VALUES: tuple[str, ...] = ("observe", "enforce")
_BOOL_VALUES: tuple[str, ...] = ("true", "false")


def valid_values_for(name: str) -> tuple[str, ...]:
    """The single source of truth for what a governed flag may be set to.

    Boolean flags (``*_ENABLED``) accept ``true``/``false``. ``ROBOTHOR_RIP_13_MODE``
    is a mode flag that only honors ``observe``/``enforce`` — the engine silently
    drops any other value, so the API must not accept the full mode ladder for it.
    Every other ``*_MODE`` flag accepts the full ladder: ``off``/``observe``/``alert``/``enforce``.

    Both the bridge's write-path validation (422 on an out-of-range value) and
    its read-path payload (``valid_values`` per flag, so the frontend doesn't
    hand-mirror this rule) import this function rather than duplicating the
    logic.
    """
    if name.endswith("_ENABLED"):
        return _BOOL_VALUES
    if name == "ROBOTHOR_RIP_13_MODE":
        return _RIP_13_VALUES
    return _MODE_VALUES


_SEED_ACTOR = "migration-084"
_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _read_db(name: str) -> str | None:
    """Return the operator-written value, or None if only a seed row / no row.

    Raises on connection failure — the caller MUST fall through to env, never
    treat a DB outage as 'off'.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value, updated_by FROM feature_flags WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        return None
    value: str | None
    value, updated_by = row
    if updated_by == _SEED_ACTOR:
        return None  # seed row == "unset", let env win during cutover
    return value


def resolve(name: str) -> str | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1] if hit[1] is not None else os.environ.get(name)
    db_val: str | None
    try:
        db_val = _read_db(name)
    except Exception:
        db_val = None  # DB unreachable -> fall through to env below
    with _lock:
        _cache[name] = (now, db_val)
    return db_val if db_val is not None else os.environ.get(name)


def set_flag(name: str, value: str, actor: str, reason: str) -> None:
    if name not in GOVERNED_FLAGS:
        raise ValueError(f"{name} is not a governed flag")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM feature_flags WHERE name = %s", (name,))
        prev = cur.fetchone()
        from_value = prev[0] if prev else None
        cur.execute(
            "INSERT INTO feature_flags (name, value, updated_by, reason) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
            "updated_by = EXCLUDED.updated_by, updated_at = now(), reason = EXCLUDED.reason",
            (name, value, actor, reason),
        )
        cur.execute(
            "INSERT INTO feature_flag_audit (name, from_value, to_value, actor, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, from_value, value, actor, reason),
        )
        cur.execute("NOTIFY feature_flags")
        conn.commit()
    invalidate()


def start_listener() -> None:
    """Hook for the daemon to attach a ``LISTEN feature_flags`` connection that
    calls :func:`invalidate` on notification, so writes on one process are
    picked up by others faster than the TTL alone.

    The TTL cache is the correctness guarantee (stale reads self-heal within
    ``_TTL_SECONDS``); this listener is a latency optimization on top of it.
    The full LISTEN/NOTIFY wiring — background thread, reconnect-on-drop — is
    built and exercised end-to-end in Task 7. Present now so callers have a
    stable import target during the cutover.
    """
    raise NotImplementedError("start_listener is wired up by the daemon in Task 7")
