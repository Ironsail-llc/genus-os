"""Local email + password sign-in, with mandatory owner MFA.

Day one, an operator has no identity provider. Before this module the Helm's
sign-in page had no buttons unless OIDC or Cloudflare Access was already
configured, and the only way in was ``GENUS_INSECURE_DEV_MODE`` — an escape
hatch that is not a sign-in method. Local login makes the five-minute install
possible; everything here exists to make a password endpoint safe to expose.

The rules, and why each one is here:

* **One failure message.** Unknown email, wrong password, disabled account and
  locked account all answer ``"invalid credentials"``. A distinguishable
  refusal is a user-enumeration oracle, and the unknown-email path still runs
  a real argon2 verification so the *timing* does not leak it either.
* **The only distinguishable state is ``mfa_required``**, and it is reachable
  only after a correct password — so it tells an attacker nothing they did not
  already know.
* **Lockout.** Ten consecutive failures freeze the account for fifteen
  minutes. Retrying while frozen does not push the clock further out (that
  would let an attacker keep a real user locked out indefinitely).
* **Rate limit.** Five attempts per (email, IP) per minute, ahead of the
  database, so a throttled attempt costs no argon2 hash and is not charged
  against the lockout counter.
* **Owner MFA.** While local login is on, an owner account with no live second
  factor is told to enroll one — regardless of what else is configured, because
  the variables that looked like "another sign-in method exists" did not mean
  that (see ``mfa_setup_required_for``). ``GENUS_OWNER_MFA_REQUIRED=false``
  opts out explicitly. ``mfa_setup_required`` is a flag on a successful
  sign-in, not a refusal: locking the operator out of their own appliance to
  enforce a policy is worse than the policy.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from robothor.auth import accounts, mfa_secrets, totp
from robothor.auth.passwords import hash_password, needs_rehash, verify_password
from robothor.auth.runtime import local_login_enabled
from robothor.db.connection import get_connection

__all__ = [
    "GENERIC_FAILURE",
    "MFA_REQUIRED",
    "LoginResult",
    "EnrollmentDeniedError",
    "MfaAlreadyEnabledError",
    "WeakPasswordError",
    "audit_subject",
    "authenticate",
    "begin_enrollment",
    "change_password",
    "confirm_enrollment",
    "disable_mfa",
    "flood_limited",
    "local_login_enabled",
    "mfa_setup_required_for",
    "reset_mfa",
    "reset_rate_limiter",
    "set_password",
    "throttled",
    "validate_password",
]

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "invalid credentials"
MFA_REQUIRED = "mfa_required"

LOCKOUT_THRESHOLD = 10
LOCKOUT_SECONDS = 15 * 60

RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60
# The limiter is keyed by (email, ip), which an attacker controls, so the map
# is capped: without this, cycling fabricated emails is a memory-exhaustion
# lever on the bridge.
RATE_LIMIT_MAX_KEYS = 10_000

MIN_PASSWORD_LENGTH = 12
# argon2 hashes whatever it is handed; a megabyte "password" is a free CPU
# burn for an unauthenticated caller.
MAX_PASSWORD_LENGTH = 1024

# The whole request body on a credential route. The field ceilings above are
# enforced AFTER the body is deserialised, so without this a single large POST
# was a cheaper memory lever than argon2: 8 KiB is two orders of magnitude more
# than the largest legitimate body (email + password + a six-digit code + a
# 64-character refresh token).
MAX_CREDENTIAL_BODY_BYTES = 8 * 1024

# ── the aggregate ceiling ────────────────────────────────────────────
# The (email, ip) window above constrains REPETITION of one pair, and both
# halves of that key come from the caller: a fabricated email is a fresh quota,
# and every request with an unseen email spends a full argon2id verification
# (64 MiB, ~50 ms) on a PUBLIC route. 40 anyio worker threads × 64 MiB is a
# 2.5 GiB, 160-thread lever on the process that mints every session in the
# appliance, and the lockout cannot help: an unknown email is never counted.
#
# So: two buckets that do not depend on any caller-supplied value — the TCP
# peer address, read before X-Client-IP is consulted, and a process-wide total.
#
# On the appliance the dashboard is usually the ONLY peer (it calls the bridge
# server-side), so the per-peer bucket is in practice an appliance-wide ceiling
# on credential attempts per minute. That is the intended trade: 30 refusals a
# minute is a 429, while argon2 saturation is an outage of every sign-in method
# at once. The global bucket is what still bounds a spray arriving from many
# peers (a directly-exposed bridge, several proxies, a botnet).
FLOOD_WINDOW_SECONDS = 60
FLOOD_PEER_ATTEMPTS = 30
FLOOD_GLOBAL_ATTEMPTS = 300
# Peer addresses are caller-supplied when the bridge is directly exposed, so
# this map is capped for the same reason _ATTEMPTS is.
FLOOD_MAX_PEERS = 10_000

_NIL_UUID = "00000000-0000-0000-0000-000000000000"

# A fixed argon2 hash no password will ever match. Verifying against it on the
# unknown-email path costs the same as verifying a real one, so response time
# does not answer "does this account exist?".
#
# Computed lazily and cached: argon2 is deliberately expensive, and paying for
# it at IMPORT made every `genus` invocation, every CLI test collection and
# every worker start carry ~50 ms for a value most of them never read.
_dummy_hash_cache: str | None = None


def _dummy_hash() -> str:
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = hash_password("local-login-timing-equalizer")
    return _dummy_hash_cache


class WeakPasswordError(ValueError):
    """The supplied password does not meet the minimum policy."""


class MfaAlreadyEnabledError(RuntimeError):
    """Enrollment was attempted on an account that already has a live factor."""


class EnrollmentDeniedError(RuntimeError):
    """Enrollment was attempted without the account's current password."""


@dataclass(frozen=True)
class LoginResult:
    """The outcome of one local sign-in attempt.

    ``ok`` and ``mfa_required`` are the only two states a caller may describe
    to the browser; ``error`` is always one of the two constants above.
    """

    ok: bool = False
    mfa_required: bool = False
    rate_limited: bool = False
    mfa_setup_required: bool = False
    tokens: dict[str, Any] | None = None
    error: str = GENERIC_FAILURE


def mfa_setup_required_for(account_row: dict[str, Any]) -> bool:
    """Whether this account must enroll a second factor before it is safe.

    The rule is deliberately blunt: an ``owner`` with no live factor, while
    local login is on. Fail closed, because the two predicates this used to ask
    instead did not answer the question the policy asks —

    * ``GENUS_OIDC_ISSUERS`` is the BRIDGE's allowlist of issuers whose tokens
      it will accept, not a sign-in method. A human can only sign in with OIDC
      when the DASHBOARD has ``AUTH_OIDC_ISSUER`` *and* ``AUTH_OIDC_CLIENT_ID``,
      a disjoint pair of variables. So a stale issuer — or the one
      ``infra/robothor.env.example`` tells the operator to add for Cloudflare
      Access — turned the policy off while the sign-in page still offered
      nothing but email and password.
    * ``CF_ACCESS_TEAM_DOMAIN`` / ``CF_ACCESS_AUD`` are dashboard-only secrets
      (see the chart README's secret classes), unset in this process, so that
      branch was dead under Helm anyway.

    ``GENUS_OWNER_MFA_REQUIRED=false`` is the escape hatch, and an operator has
    to type it. It is advisory either way: this flag raises a banner and a panel
    message, never a refusal — locking the operator out of their own appliance
    to enforce a policy is worse than the policy.
    """
    if account_row.get("role") != "owner" or account_row.get("mfa_enabled"):
        return False
    if not local_login_enabled():
        # No password endpoint, so there is no password for a second factor to
        # sit behind: whatever gets this account in, it is not local login.
        return False
    from robothor.settings import get_settings

    return bool(get_settings().auth.owner_mfa_required)


# ── rate limiter ─────────────────────────────────────────────────────
# In-process only, which is correct for the single-bridge appliance this
# ships as. FOLLOW-UP for multi-replica deployments: move the window into
# Redis (robothor/events/bus.py already owns a connection) so N replicas
# cannot each grant a full quota.

_ATTEMPTS: dict[tuple[str, str], deque[float]] = {}

# The bridge's credential handlers are sync ``def`` ON PURPOSE, so FastAPI runs
# them in its worker threadpool (40 threads by default) where argon2 and
# psycopg2 belong. That makes every function below re-entrant from many threads
# at once, and ``_ATTEMPTS`` is shared mutable state.
#
# Unlocked, ``_prune`` iterated the live dict while other threads inserted into
# it and raised ``RuntimeError: dictionary changed size during iteration`` out
# of the sign-in route — a 500 on POST /api/auth/login under exactly the flood
# the key cap exists to survive. ``sorted(_ATTEMPTS)`` had the same problem,
# plus a KeyError if another thread popped the key and an IndexError on a deque
# another thread had just emptied.
#
# One lock held across the whole of ``_rate_limited`` (so it also covers
# ``_prune``). The critical section is a handful of dict and deque operations;
# contention is irrelevant next to the ~50 ms argon2 verification that follows.
_ATTEMPTS_LOCK = threading.Lock()


_PEER_ATTEMPTS: dict[str, deque[float]] = {}
_GLOBAL_ATTEMPTS: deque[float] = deque()
_FLOOD_LOCK = threading.Lock()
_flood_alarm_at: float = 0.0


def reset_rate_limiter() -> None:
    """Forget every recorded attempt. For tests; production never calls it."""
    global _flood_alarm_at
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.clear()
    with _FLOOD_LOCK:
        _PEER_ATTEMPTS.clear()
        _GLOBAL_ATTEMPTS.clear()
        _flood_alarm_at = 0.0


def flood_limited(peer: str | None) -> bool:
    """Record one credential attempt from *peer* and say whether to refuse it.

    The coarse ceiling in front of argon2 and the database, checked before the
    per-(email, IP) window and before any account is loaded. *peer* must be the
    address the TCP connection came from — ``request.client.host``, NOT the
    forwarded ``X-Client-IP`` — or the one control that does not depend on a
    caller-supplied value depends on a caller-supplied value.

    Deliberately NOT audited per refusal: this fires exactly when request volume
    IS the attack, so a row per refusal would turn a flood into a log
    amplifier. It logs once per window instead, because a control that refuses
    silently is indistinguishable from an outage.
    """
    global _flood_alarm_at

    now = time.monotonic()
    cutoff = now - FLOOD_WINDOW_SECONDS
    key = peer or "-"

    with _FLOOD_LOCK:
        while _GLOBAL_ATTEMPTS and _GLOBAL_ATTEMPTS[0] < cutoff:
            _GLOBAL_ATTEMPTS.popleft()

        window = _PEER_ATTEMPTS.get(key)
        if window is None:
            if len(_PEER_ATTEMPTS) >= FLOOD_MAX_PEERS:
                _prune_peers(cutoff)
            window = _PEER_ATTEMPTS.setdefault(key, deque())
        while window and window[0] < cutoff:
            window.popleft()

        over_peer = len(window) >= FLOOD_PEER_ATTEMPTS
        if over_peer or len(_GLOBAL_ATTEMPTS) >= FLOOD_GLOBAL_ATTEMPTS:
            alarm = now - _flood_alarm_at >= FLOOD_WINDOW_SECONDS
            if alarm:
                _flood_alarm_at = now
            scope = "one peer" if over_peer else "this process"
            if alarm:
                # No peer address, no email, no body: identifiers only, and the
                # scope is one of two literals.
                logger.warning(
                    "local login: credential requests from %s are over the aggregate "
                    "limit and are being refused with 429",
                    scope,
                )
            return True

        window.append(now)
        _GLOBAL_ATTEMPTS.append(now)
        return False


def _prune_peers(cutoff: float) -> None:
    """Bound the peer map. Called with ``_FLOOD_LOCK`` held."""
    for key, window in list(_PEER_ATTEMPTS.items()):
        if not window or window[-1] < cutoff:
            _PEER_ATTEMPTS.pop(key, None)
    if len(_PEER_ATTEMPTS) >= FLOOD_MAX_PEERS:
        snapshot = list(_PEER_ATTEMPTS)
        snapshot.sort(key=lambda k: (_PEER_ATTEMPTS.get(k) or deque([float("-inf")]))[-1])
        for key in snapshot[: FLOOD_MAX_PEERS // 10]:
            _PEER_ATTEMPTS.pop(key, None)


def throttled(key: str, ip: str | None) -> bool:
    """Public face of the limiter for the *authenticated* credential routes.

    ``/mfa/confirm`` and ``/mfa/disable`` take a six-digit code, and
    ``/password`` takes the current password. Requiring a session is not a
    rate limit: one stolen cookie would otherwise buy a million guesses. Keyed
    on a caller-supplied namespace + user id rather than an email so these
    routes cannot consume the sign-in quota of the same account.
    """
    return _rate_limited(key, ip)


def _rate_limited(email: str, ip: str | None) -> bool:
    """Record an attempt and return whether this one is over the limit.

    The whole body runs under ``_ATTEMPTS_LOCK``: the map and the deque inside
    it are shared across the bridge's worker threads, and read-modify-write on
    either is not atomic.
    """
    key = (email, ip or "-")
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS

    with _ATTEMPTS_LOCK:
        window = _ATTEMPTS.get(key)
        if window is None:
            if len(_ATTEMPTS) >= RATE_LIMIT_MAX_KEYS:
                _prune(cutoff)
            window = _ATTEMPTS.setdefault(key, deque())
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= RATE_LIMIT_ATTEMPTS:
            return True
        window.append(now)
        return False


def _prune(cutoff: float) -> None:
    """Drop keys with no attempt inside the window; then, if the map is still
    at its cap, drop the oldest keys outright so it can never grow past it.

    Called with ``_ATTEMPTS_LOCK`` held. Both passes take a SNAPSHOT
    (``list(...)``) rather than iterating the live map, and the sort key uses
    ``.get()`` with an emptiness guard: even under the lock, a snapshot is the
    shape that cannot raise if this is ever called from somewhere else.
    """
    for key, window in list(_ATTEMPTS.items()):
        if not window or window[-1] < cutoff:
            _ATTEMPTS.pop(key, None)
    if len(_ATTEMPTS) >= RATE_LIMIT_MAX_KEYS:
        snapshot = list(_ATTEMPTS)
        snapshot.sort(key=_last_attempt)
        for key in snapshot[: RATE_LIMIT_MAX_KEYS // 10]:
            _ATTEMPTS.pop(key, None)


def _last_attempt(key: tuple[str, str]) -> float:
    """Sort key: the most recent attempt on *key*, or "infinitely old"."""
    window = _ATTEMPTS.get(key)
    return window[-1] if window else float("-inf")


# ── DAL ──────────────────────────────────────────────────────────────


def _load_account(tenant_id: str, email: str) -> dict[str, Any] | None:
    return accounts.get_account_by_email(tenant_id, email)


def _load_account_by_id(user_id: str) -> dict[str, Any] | None:
    return accounts.get_account_by_id(user_id)


# The lockout statement, with the table name left as a format slot so the
# concurrency test can run the REAL text against a throwaway table.
#
# Every arithmetic operand is a COLUMN REFERENCE, and that is the whole point.
# The first version computed the next value in a CTE:
#
#     WITH next AS (SELECT failed_login_count + 1 AS n FROM user_accounts WHERE id = ...)
#     UPDATE user_accounts SET failed_login_count = (SELECT n FROM next) ...
#
# A subquery is evaluated against the statement's snapshot. When the UPDATE
# then blocks on a concurrent writer's row lock, PostgreSQL re-runs the scan
# under EvalPlanQual against the newly committed row — but the CTE's already
# materialised output is not recomputed. Eight overlapping failures therefore
# all wrote 1, and a parallel password spray could hold the counter below the
# threshold indefinitely while the lockout never fired. A bare column
# reference IS re-evaluated under EPQ, so the increment survives. Proven both
# ways in robothor/auth/tests/test_local_login_concurrency.py.
#
# Parameters, in order: LOCKOUT_THRESHOLD, LOCKOUT_SECONDS, user_id.
FAILURE_UPDATE_SQL = """
    UPDATE {table} SET
        failed_login_count = CASE
            WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
            ELSE failed_login_count + 1
        END,
        locked_until = CASE
            WHEN (CASE
                      WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                      ELSE failed_login_count + 1
                  END) >= %s
            THEN NOW() + make_interval(secs => %s)
            ELSE NULL
        END,
        updated_at = NOW()
    WHERE id = %s
    RETURNING failed_login_count, locked_until
"""

# A write of the same shape that changes nothing. Every failure branch issues
# one so the response time does not say which branch it was — see
# ``_equal_cost_failure``.
NOOP_UPDATE_SQL = """
    UPDATE user_accounts SET updated_at = updated_at
    WHERE id = %s AND FALSE
    RETURNING failed_login_count, locked_until
"""


def _record_failure(user_id: str) -> None:
    """Increment the consecutive-failure counter, locking at the threshold.

    One statement, so two concurrent failures cannot both read ``9`` and both
    write ``10``. A lock that has already expired restarts the count at 1
    rather than resuming at 10 — otherwise a single mistyped password after
    the freeze lifts would re-freeze the account immediately.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            FAILURE_UPDATE_SQL.format(table="user_accounts"),
            (LOCKOUT_THRESHOLD, LOCKOUT_SECONDS, user_id),
        )
        cur.fetchone()
        conn.commit()


def _record_noop(user_id: str | None) -> None:
    """A write that matches no row, for failure branches with nothing to count.

    Without it the wrong-password branch performs an UPDATE and a COMMIT that
    the unknown-email, disabled and locked branches do not — a difference an
    attacker can time, which is exactly the enumeration oracle the single
    failure message exists to close.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(NOOP_UPDATE_SQL, (user_id or _NIL_UUID,))
        cur.fetchone()
        conn.commit()


# Claim one TOTP step, or claim nothing. The predicate and the write are the
# SAME statement, so the database decides the winner.
#
# Reading the watermark and then writing it was a check-then-write across two
# connections: two requests carrying the same code both read "nothing spent",
# both verified, and both signed in. A one-time password that works twice at
# once is not one-time. Row-level locking serialises the two UPDATEs, the
# predicate is re-evaluated under EvalPlanQual against the committed row, and
# the loser gets ``rowcount == 0`` — which every caller reads as "replay".
#
# The ``<`` also refuses an OLDER step, so a late arrival cannot walk the
# watermark backwards and re-open a window that was already spent.
#
# Parameters, in order: step (to write), user_id, step (to compare).
BURN_STEP_SQL = """
    UPDATE {table}
       SET mfa_last_used_step = %s, updated_at = NOW()
     WHERE id = %s
       AND (mfa_last_used_step IS NULL OR mfa_last_used_step < %s)
"""


def _record_success(user_id: str, *, mfa_step: int | None = None) -> bool:
    """Clear the failure state, and claim the TOTP step that was just spent.

    One statement, so the step cannot be recorded separately from the sign-in
    it belongs to — a crash between the two would leave the code replayable —
    and so two simultaneous uses of one code cannot both win.

    Returns False when the step had already been claimed: the caller must
    refuse that sign-in even though the password and the code were correct.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if mfa_step is None:
            cur.execute(
                "UPDATE user_accounts SET failed_login_count = 0, locked_until = NULL, "
                "last_login_at = NOW(), updated_at = NOW() WHERE id = %s",
                (user_id,),
            )
            conn.commit()
            return True
        cur.execute(
            "UPDATE user_accounts SET failed_login_count = 0, locked_until = NULL, "
            "last_login_at = NOW(), mfa_last_used_step = %s, updated_at = NOW() "
            "WHERE id = %s AND (mfa_last_used_step IS NULL OR mfa_last_used_step < %s)",
            (mfa_step, user_id, mfa_step),
        )
        claimed = bool(cur.rowcount)
        conn.commit()
        return claimed


def _record_mfa_step(user_id: str, step: int) -> bool:
    """Claim a TOTP step outside a sign-in (enrollment confirm, MFA disable).

    Returns False when the step was already spent — a replay, which the caller
    must refuse.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(BURN_STEP_SQL.format(table="user_accounts"), (step, user_id, step))
        claimed = bool(cur.rowcount)
        conn.commit()
        return claimed


def _confirm_enrollment_atomically(user_id: str, step: int) -> bool:
    """Enable MFA and claim the confirming step in ONE transaction.

    Two statements on two connections could leave the factor enabled with the
    step unclaimed (the confirming code still usable at the sign-in page) or
    the step claimed with the factor off. One transaction has neither state.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_accounts SET mfa_enabled = TRUE, mfa_last_used_step = %s, "
            "updated_at = NOW() "
            "WHERE id = %s AND (mfa_last_used_step IS NULL OR mfa_last_used_step < %s)",
            (step, user_id, step),
        )
        claimed = bool(cur.rowcount)
        if not claimed:
            conn.rollback()
            return False
        conn.commit()
        return True


def _store_password_hash(user_id: str, password_hash: str) -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_accounts SET password_hash = %s, password_updated_at = NOW(), "
            "failed_login_count = 0, locked_until = NULL, updated_at = NOW() WHERE id = %s",
            (password_hash, user_id),
        )
        conn.commit()


def _store_mfa_secret(user_id: str, secret_enc: str, *, enabled: bool) -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_accounts SET mfa_secret_enc = %s, mfa_enabled = %s, updated_at = NOW() "
            "WHERE id = %s",
            (secret_enc, enabled, user_id),
        )
        conn.commit()


def _set_mfa_enabled(user_id: str, enabled: bool) -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_accounts SET mfa_enabled = %s, updated_at = NOW() WHERE id = %s",
            (enabled, user_id),
        )
        conn.commit()


def _clear_mfa(user_id: str) -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_accounts SET mfa_enabled = FALSE, mfa_secret_enc = NULL, "
            "updated_at = NOW() WHERE id = %s",
            (user_id,),
        )
        conn.commit()


# ── helpers ──────────────────────────────────────────────────────────


def _is_locked(account_row: dict[str, Any]) -> bool:
    locked_until = account_row.get("locked_until")
    if not isinstance(locked_until, datetime):
        return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)
    return locked_until > datetime.now(UTC)


def _current_secret(account_row: dict[str, Any]) -> str | None:
    """The CONFIRMED TOTP secret, or None.

    ``mfa_enabled`` gates the read: an in-progress enrollment also lives in
    ``mfa_secret_enc``, and an unconfirmed secret must never be able to
    satisfy a login challenge.
    """
    if not account_row.get("mfa_enabled"):
        return None
    return mfa_secrets.decrypt_secret(
        account_row.get("mfa_secret_enc"), user_id=str(account_row.get("id") or "")
    )


def _fresh_step(account_row: dict[str, Any], secret: str | None, code: str | None) -> int | None:
    """The step *code* matches, if that step has not already been spent.

    RFC 6238 §5.2: a one-time password is accepted once. The verifier has a
    ±1 step window, so without a watermark the same six digits keep working
    for ninety seconds — a code read over a shoulder, captured from a phished
    page or replayed from a log is not "one-time" at all.
    """
    if secret is None:
        return None
    step = totp.matching_step(secret, code)
    if step is None:
        return None
    last_used = account_row.get("mfa_last_used_step")
    if isinstance(last_used, int) and step <= last_used:
        return None
    return step


def audit_subject(email: str) -> str:
    """A stable, non-reversible handle for an email, for audit rows.

    A failed sign-in must be attributable — "which account was sprayed, from
    where" is the question an incident asks first — but the audit log is
    exported to a SIEM and read by people who have no business learning that
    ``ceo@acme.example`` has an account here, especially on a FAILED attempt
    where the address may be an attacker's guess rather than a real user.

    So: HKDF-SHA256 derives a key from the server-held signing key (info
    ``b"genus-audit-email"``), and the address is HMAC'd **under** that key.

    The key belongs in the key slot. The first version concatenated the address
    and the signing key into HKDF's input keying material, which makes the
    "key" just more salt: the construction then reduces to an unkeyed hash of a
    low-entropy, fully enumerable input, and anyone holding the export plus a
    list of plausible addresses reverses it by trying them. As an HMAC key, the
    secret is what an attacker must have to compute a single handle.

    Equal addresses give equal handles, so spray patterns stay visible. Falls
    back to a fixed placeholder rather than raising if no key is resolvable —
    an audit row must never be the reason a sign-in 500s.
    """
    try:
        import hashlib
        import hmac as hmac_module

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        from robothor.auth.tokens import signing_key

        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"genus-audit-email"
        ).derive(signing_key().encode("utf-8"))
        message = accounts.canonical_email(email).encode("utf-8")
        return hmac_module.new(key, message, hashlib.sha256).hexdigest()[:32]
    except Exception:  # pragma: no cover - never break a sign-in over an audit field
        return "unavailable"


def validate_password(password: str) -> None:
    """Raise ``WeakPasswordError`` unless *password* meets the policy."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")


def _audit(action: str, *, status: str = "ok", **details: Any) -> None:
    """Audit a local-credential state change (CLI paths included).

    Identifiers only — never a password, a code, or a TOTP secret.
    """
    try:
        from robothor.audit.logger import log_event

        log_event(
            "auth.local",
            action=action,
            category="auth",
            actor="local-login",
            status=status,
            details=details,
        )
    except Exception:  # pragma: no cover - audit must never break auth
        logger.debug("local_login: audit write failed", exc_info=True)


# ── authentication ───────────────────────────────────────────────────


def authenticate(
    tenant_id: str,
    email: str,
    password: str,
    code: str | None = None,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> LoginResult:
    """Authenticate one local sign-in attempt.

    Returns a ``LoginResult``. The caller maps ``rate_limited`` to 429,
    ``mfa_required`` to a 401 naming that state, and everything else to a 401
    carrying ``GENERIC_FAILURE`` verbatim.
    """
    if not local_login_enabled():
        return LoginResult()

    normalized = accounts.canonical_email(email)
    # Namespaced so a sign-in bucket can never be the same key as one of the
    # authenticated credential routes' buckets in the shared map.
    if _rate_limited(f"login:{normalized}", ip):
        return LoginResult(rate_limited=True)

    account_row = _load_account(tenant_id, normalized)
    if account_row is None:
        # Spend the same work an existing account would, so the clock does not
        # answer a question the message refuses to: one argon2 verification and
        # one UPDATE+COMMIT, exactly as the wrong-password branch below.
        verify_password(password, _dummy_hash())
        _record_noop(None)
        return LoginResult()

    if account_row.get("status") != "active":
        verify_password(password, _dummy_hash())
        _record_noop(str(account_row["id"]))
        return LoginResult()

    if _is_locked(account_row):
        # The write is a no-op ON PURPOSE. The cost must match the other
        # branches, but an attacker must not be able to keep a real user
        # locked out by hammering a frozen account, so nothing is counted.
        verify_password(password, _dummy_hash())
        _record_noop(str(account_row["id"]))
        return LoginResult()

    stored_hash = account_row.get("password_hash")
    # An SSO-only account has password_hash IS NULL, and verify_password
    # short-circuits on a falsy hash — which would answer instantly and tell an
    # attacker exactly which accounts have no local credential. Equalize.
    if not verify_password(password, stored_hash or _dummy_hash()) or not stored_hash:
        _record_failure(str(account_row["id"]))
        return LoginResult()

    mfa_step: int | None = None
    if account_row.get("mfa_enabled"):
        secret = _current_secret(account_row)
        if code is None or not code.strip():
            # The first leg of a two-step sign-in, not an attack: no counter.
            # Still a write, so the two-step path costs what the others do.
            _record_noop(str(account_row["id"]))
            return LoginResult(mfa_required=True, error=MFA_REQUIRED)
        # A None secret means the stored ciphertext could not be opened
        # (signing-key rotation, tampering, a blob transplanted from another
        # row). A None step means a wrong code, or a REPLAY of one already
        # spent. Fail closed on all of them — never bypass.
        mfa_step = _fresh_step(account_row, secret, code)
        if mfa_step is None:
            _record_failure(str(account_row["id"]))
            return LoginResult(mfa_required=True, error=MFA_REQUIRED)

    # The step is claimed by the SAME statement that clears the failure state,
    # and the database decides who gets it. A loser here presented a correct
    # password and a correct code — and a code someone else was already using
    # this instant, which is the replay this refuses.
    if not _record_success(str(account_row["id"]), mfa_step=mfa_step):
        _record_failure(str(account_row["id"]))
        return LoginResult(mfa_required=True, error=MFA_REQUIRED)
    if isinstance(stored_hash, str) and needs_rehash(stored_hash):
        try:
            _store_password_hash(str(account_row["id"]), hash_password(password))
        except Exception:  # pragma: no cover - an upgrade must not fail a login
            logger.warning("local_login: password rehash failed for an account", exc_info=True)

    issued = accounts.issue_for_account(account_row, user_agent=user_agent, ip=ip)
    return LoginResult(
        ok=True,
        tokens=issued,
        mfa_setup_required=mfa_setup_required_for(account_row),
        error="",
    )


# ── credential management ────────────────────────────────────────────


def set_password(user_id: str, new_password: str) -> None:
    """Set (or replace) an account's password. No current-password check —
    for the operator CLI and administrative reset only."""
    validate_password(new_password)
    _store_password_hash(user_id, hash_password(new_password))
    accounts.revoke_user_sessions(user_id)
    _audit("password.set", user_id=user_id)


def change_password(
    user_id: str,
    current_password: str,
    new_password: str,
    *,
    keep_refresh_hash: str | None = None,
) -> bool:
    """Self-service password change. False when ``current_password`` is wrong.

    Every OTHER refresh session is revoked on success: a password change that
    leaves a stolen refresh token working has not locked anyone out. The
    caller's own session is spared when ``keep_refresh_hash`` names it, so the
    operator is not thrown out of the page they are standing on — which would
    train people to avoid changing their password, and is not a security gain
    when they have just proven they hold the old one.
    """
    validate_password(new_password)
    account_row = _load_account_by_id(user_id)
    if not account_row or account_row.get("status") != "active":
        return False
    if not verify_password(current_password, account_row.get("password_hash")):
        _audit("password.change", status="denied", user_id=user_id)
        return False
    _store_password_hash(user_id, hash_password(new_password))
    accounts.revoke_user_sessions(user_id, except_refresh_hash=keep_refresh_hash)
    _audit("password.change", user_id=user_id, kept_current_session=bool(keep_refresh_hash))
    return True


def begin_enrollment(user_id: str, password: str) -> dict[str, str]:
    """Provision a PENDING TOTP secret and return what the app must display.

    Requires the account's CURRENT PASSWORD. Binding a second factor is a
    change of authority, and a session cookie alone must not be enough to
    perform it: otherwise an attacker holding a stolen session enrols their
    own authenticator, and from then on the real owner's password is not
    sufficient to get back in.

    The secret is returned exactly once, here, because an authenticator has to
    receive it. It is stored encrypted (bound to this row — see
    ``mfa_secrets``) and ``mfa_enabled`` stays false until
    ``confirm_enrollment`` proves the operator's app holds the same seed — so
    a half-finished enrollment can never lock anyone out.

    Refuses outright when a CONFIRMED factor already exists. Without that, a
    hijacked session could POST /mfa/enroll and the write would replace the
    victim's live secret with a pending one — silently turning their second
    factor OFF, which is the one thing /mfa/disable deliberately demands both
    a password and a live code to do.
    """
    account_row = _load_account_by_id(user_id)
    if not account_row or account_row.get("status") != "active":
        raise EnrollmentDeniedError("no active account")
    if account_row.get("mfa_enabled"):
        raise MfaAlreadyEnabledError("this account already has a confirmed second factor")
    if not verify_password(password, account_row.get("password_hash") or _dummy_hash()):
        _audit("mfa.enroll.begin", status="denied", user_id=user_id)
        raise EnrollmentDeniedError("current password required")
    email = str(account_row.get("email") or "")
    secret = totp.new_secret()
    _store_mfa_secret(user_id, mfa_secrets.encrypt_secret(secret, user_id=user_id), enabled=False)
    _audit("mfa.enroll.begin", user_id=user_id)
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, email=email)}


def confirm_enrollment(user_id: str, code: str) -> bool:
    """Enable MFA once *code* proves the pending secret was received.

    The accepted step is burned along with the enable, so the code that armed
    the factor cannot immediately be replayed at the sign-in page.
    """
    account_row = _load_account_by_id(user_id)
    if not account_row:
        return False
    secret = mfa_secrets.decrypt_secret(account_row.get("mfa_secret_enc"), user_id=user_id)
    step = _fresh_step(account_row, secret, code)
    if step is None:
        _audit("mfa.enroll.confirm", status="denied", user_id=user_id)
        return False
    # Enable and claim in one transaction — never a state where the factor is
    # on but the confirming code is still spendable at the sign-in page.
    if not _confirm_enrollment_atomically(user_id, step):
        _audit("mfa.enroll.confirm", status="denied", user_id=user_id)
        return False
    _audit("mfa.enroll.confirm", user_id=user_id)
    return True


def disable_mfa(user_id: str, password: str, code: str) -> bool:
    """Turn MFA off. Requires BOTH the password and a live code, so a stolen
    session cookie alone cannot strip the second factor."""
    account_row = _load_account_by_id(user_id)
    if not account_row:
        return False
    if not verify_password(password, account_row.get("password_hash")):
        _audit("mfa.disable", status="denied", user_id=user_id)
        return False
    step = _fresh_step(account_row, _current_secret(account_row), code)
    if step is None:
        _audit("mfa.disable", status="denied", user_id=user_id)
        return False
    # Claim FIRST. Two simultaneous requests carrying the same code must not
    # both count as "proved the second factor", and the loser must not get to
    # strip the factor on the strength of a code someone else just spent.
    if not _record_mfa_step(user_id, step):
        _audit("mfa.disable", status="denied", user_id=user_id)
        return False
    _clear_mfa(user_id)
    _audit("mfa.disable", user_id=user_id)
    return True


def reset_mfa(user_id: str) -> None:
    """Administratively clear MFA (operator CLI recovery path)."""
    _clear_mfa(user_id)
    _audit("mfa.reset", user_id=user_id)
