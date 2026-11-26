"""Whether a human token's account still stands where the token says.

An access token is self-contained and lives fifteen minutes. Revoking an
account's sessions stops the next refresh, not the access token already in a
browser — so an account moved to another tenant (or disabled) kept acting in
its old tenant until the token expired. The bridge therefore honours a HUMAN
token only while its account still lives in the token's home tenant
(``home_tid``) and is ``active``.

One SELECT per (user, home tenant) per :data:`TTL_SECONDS`, cached in this
process — the bridge's and each engine's. The cache
bounds how stale the answer can be for a change made by ANOTHER process (``genus
tenant move-account``, an operator's psql); a change the bridge makes itself
calls :func:`forget` and takes effect on the very next request.

The lookup runs bound to the HOME tenant (``tenant_scope``): with RLS on, a
moved account is invisible there and reads as absent; with RLS off, the row's
own ``tenant_id`` disagrees with the token. Either way the token is refused.

A lookup that FAILS (database unreachable) is not cached and is treated as
standing, with a warning: a bridge that cannot read the database cannot serve
the request's data either, and failing closed would sign every operator out on
a connection blip.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.auth.deps import AuthContext

logger = logging.getLogger(__name__)

#: How long one answer is trusted for a change made outside this process.
TTL_SECONDS = 30.0

_lock = threading.Lock()
#: (user_id, home tenant) -> (expires_at, tenant the account lives in or None,
#: status). Keyed by the HOME tenant as well: the lookup is bound to it, so
#: under RLS a moved account reads as absent from its old home and present from
#: its new one — an old token's "absent" must not sign out the new token.
_cache: dict[tuple[str, str], tuple[float, str | None, str]] = {}


def _now() -> float:
    return time.monotonic()


def _lookup(user_id: str, home_tenant_id: str) -> tuple[str | None, str]:
    from robothor.auth import accounts
    from robothor.db.connection import tenant_scope

    with tenant_scope(home_tenant_id):
        row = accounts.get_account_by_id(user_id)
    if not row:
        return None, ""
    return str(row.get("tenant_id") or ""), str(row.get("status") or "")


def stands(user_id: str, home_tenant_id: str) -> bool:
    """True while the account lives in ``home_tenant_id`` and is active."""
    now = _now()
    key = (user_id, home_tenant_id)
    with _lock:
        cached = _cache.get(key)
    if cached is None or cached[0] <= now:
        try:
            tenant, status = _lookup(user_id, home_tenant_id)
        except Exception as error:  # noqa: BLE001 — see the module docstring
            logger.warning("Could not check a session's account: %s", type(error).__name__)
            return True
        cached = (now + TTL_SECONDS, tenant, status)
        with _lock:
            _cache[key] = cached
    _expires, tenant, status = cached
    return tenant == home_tenant_id and status == "active"


def refusal(auth: AuthContext) -> tuple[int, str] | None:
    """Why a verified token may not be honoured right now, as (status, reason), or None.

    A signature proves who minted the token, not that what it says is still
    true. Two things can stop being true before it expires:

    * the switch was turned off — an ACTING token is honoured only while
      ``GENUS_TENANT_SWITCH_MODE`` is ``enforce`` (403), so turning it off ends
      every acting session at once;
    * the account moved tenant or was disabled — a HUMAN token is honoured
      only while its account still lives in ``home_tid`` and is active (401;
      :func:`stands`).

    Shared by every service that verifies these tokens (the bridge, the
    orchestrator), so they cannot disagree about whether a session stands.
    Synchronous: the standing lookup may read the database, so an async caller
    runs this through ``asyncio.to_thread``.
    """
    from robothor.auth.runtime import tenant_switch_mode

    if auth.is_acting and tenant_switch_mode() != "enforce":
        return 403, "tenant switching disabled"
    if not auth.is_service and not stands(auth.user_id, auth.home_tenant_id):
        return 401, "session no longer valid"
    return None


def forget(user_id: str) -> None:
    """Drop every cached answer for one account, whatever its home: it just changed."""
    user_id = str(user_id)
    with _lock:
        for key in [key for key in _cache if key[0] == user_id]:
            del _cache[key]


def forget_all() -> None:
    """Drop every cached answer (tests)."""
    with _lock:
        _cache.clear()
