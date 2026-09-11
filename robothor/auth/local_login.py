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
* **Owner MFA.** When local login is the only configured method, the owner
  account is told to enroll a second factor. ``mfa_setup_required`` is a flag
  on a successful sign-in, not a refusal: locking the operator out of their
  own appliance to enforce a policy is worse than the policy.
"""

from __future__ import annotations

import logging
import os
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
    "MfaAlreadyEnabledError",
    "WeakPasswordError",
    "authenticate",
    "begin_enrollment",
    "change_password",
    "confirm_enrollment",
    "disable_mfa",
    "local_login_enabled",
    "mfa_setup_required_for",
    "other_providers_configured",
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

# A fixed argon2 hash no password will ever match. Verifying against it on the
# unknown-email path costs the same as verifying a real one, so response time
# does not answer "does this account exist?".
_DUMMY_HASH = hash_password("local-login-timing-equalizer")


class WeakPasswordError(ValueError):
    """The supplied password does not meet the minimum policy."""


class MfaAlreadyEnabledError(RuntimeError):
    """Enrollment was attempted on an account that already has a live factor."""


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


def other_providers_configured() -> bool:
    """Whether a sign-in method other than local login exists.

    OIDC issuers, or a Cloudflare Access team domain + audience. Used only to
    decide whether owner MFA is *mandatory*: with a second method configured,
    the operator has another way back in and the appliance is not betting
    everything on one password.
    """
    if any(item.strip() for item in os.environ.get("GENUS_OIDC_ISSUERS", "").split(",")):
        return True
    return bool(
        os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip()
        and os.environ.get("CF_ACCESS_AUD", "").strip()
    )


def mfa_setup_required_for(account_row: dict[str, Any]) -> bool:
    """Whether this account must enroll a second factor before it is safe."""
    return bool(
        account_row.get("role") == "owner"
        and not account_row.get("mfa_enabled")
        and not other_providers_configured()
    )


# ── rate limiter ─────────────────────────────────────────────────────
# In-process only, which is correct for the single-bridge appliance this
# ships as. FOLLOW-UP for multi-replica deployments: move the window into
# Redis (robothor/events/bus.py already owns a connection) so N replicas
# cannot each grant a full quota.

_ATTEMPTS: dict[tuple[str, str], deque[float]] = {}


def reset_rate_limiter() -> None:
    """Forget every recorded attempt. For tests; production never calls it."""
    _ATTEMPTS.clear()


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
    """Record an attempt and return whether this one is over the limit."""
    key = (email, ip or "-")
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS

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
    at its cap, drop the oldest keys outright so it can never grow past it."""
    for key in [k for k, w in _ATTEMPTS.items() if not w or w[-1] < cutoff]:
        _ATTEMPTS.pop(key, None)
    if len(_ATTEMPTS) >= RATE_LIMIT_MAX_KEYS:
        for key in sorted(_ATTEMPTS, key=lambda k: _ATTEMPTS[k][-1])[: RATE_LIMIT_MAX_KEYS // 10]:
            _ATTEMPTS.pop(key, None)


# ── DAL ──────────────────────────────────────────────────────────────


def _load_account(tenant_id: str, email: str) -> dict[str, Any] | None:
    return accounts.get_account_by_email(tenant_id, email)


def _load_account_by_id(user_id: str) -> dict[str, Any] | None:
    return accounts.get_account_by_id(user_id)


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
            """
            WITH next AS (
                SELECT CASE
                           WHEN locked_until IS NOT NULL AND locked_until <= NOW() THEN 1
                           ELSE failed_login_count + 1
                       END AS n
                FROM user_accounts WHERE id = %s
            )
            UPDATE user_accounts SET
                failed_login_count = (SELECT n FROM next),
                locked_until = CASE
                    WHEN (SELECT n FROM next) >= %s THEN NOW() + make_interval(secs => %s)
                    ELSE NULL
                END,
                updated_at = NOW()
            WHERE id = %s
            RETURNING failed_login_count, locked_until
            """,
            (user_id, LOCKOUT_THRESHOLD, LOCKOUT_SECONDS, user_id),
        )
        cur.fetchone()
        conn.commit()


def _record_success(user_id: str) -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_accounts SET failed_login_count = 0, locked_until = NULL, "
            "last_login_at = NOW(), updated_at = NOW() WHERE id = %s",
            (user_id,),
        )
        conn.commit()


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
    return mfa_secrets.decrypt_secret(account_row.get("mfa_secret_enc"))


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

    normalized = (email or "").strip().casefold()
    # Namespaced so a sign-in bucket can never be the same key as one of the
    # authenticated credential routes' buckets in the shared map.
    if _rate_limited(f"login:{normalized}", ip):
        return LoginResult(rate_limited=True)

    account_row = _load_account(tenant_id, normalized)
    if account_row is None:
        # Spend the same work an existing account would, so the clock does not
        # answer a question the message refuses to.
        verify_password(password, _DUMMY_HASH)
        return LoginResult()

    if account_row.get("status") != "active":
        verify_password(password, _DUMMY_HASH)
        return LoginResult()

    if _is_locked(account_row):
        # Deliberately no counter write: an attacker must not be able to keep
        # a real user locked out by hammering a frozen account.
        verify_password(password, _DUMMY_HASH)
        return LoginResult()

    stored_hash = account_row.get("password_hash")
    # An SSO-only account has password_hash IS NULL, and verify_password
    # short-circuits on a falsy hash — which would answer instantly and tell an
    # attacker exactly which accounts have no local credential. Equalize.
    if not verify_password(password, stored_hash or _DUMMY_HASH) or not stored_hash:
        _record_failure(str(account_row["id"]))
        return LoginResult()

    if account_row.get("mfa_enabled"):
        secret = _current_secret(account_row)
        if code is None or not code.strip():
            # The first leg of a two-step sign-in, not an attack: no counter.
            return LoginResult(mfa_required=True, error=MFA_REQUIRED)
        # secret is None only when the stored ciphertext cannot be opened
        # (signing-key rotation, tampering). Fail closed — never bypass.
        if secret is None or not totp.verify(secret, code):
            _record_failure(str(account_row["id"]))
            return LoginResult(mfa_required=True, error=MFA_REQUIRED)

    _record_success(str(account_row["id"]))
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


def change_password(user_id: str, current_password: str, new_password: str) -> bool:
    """Self-service password change. False when ``current_password`` is wrong.

    Every existing refresh session is revoked on success: a password change
    that leaves a stolen refresh token working has not locked anyone out.
    """
    validate_password(new_password)
    account_row = _load_account_by_id(user_id)
    if not account_row or account_row.get("status") != "active":
        return False
    if not verify_password(current_password, account_row.get("password_hash")):
        _audit("password.change", status="denied", user_id=user_id)
        return False
    _store_password_hash(user_id, hash_password(new_password))
    accounts.revoke_user_sessions(user_id)
    _audit("password.change", user_id=user_id)
    return True


def begin_enrollment(user_id: str, email: str) -> dict[str, str]:
    """Provision a PENDING TOTP secret and return what the app must display.

    The secret is returned exactly once, here, because an authenticator has to
    receive it. It is stored encrypted and ``mfa_enabled`` stays false until
    ``confirm_enrollment`` proves the operator's app holds the same seed — so
    a half-finished enrollment can never lock anyone out.

    Refuses outright when a CONFIRMED factor already exists. Without that, a
    hijacked session could POST /mfa/enroll and the write would replace the
    victim's live secret with a pending one — silently turning their second
    factor OFF, which is the one thing /mfa/disable deliberately demands both
    a password and a live code to do.
    """
    existing = _load_account_by_id(user_id)
    if existing and existing.get("mfa_enabled"):
        raise MfaAlreadyEnabledError("this account already has a confirmed second factor")
    secret = totp.new_secret()
    _store_mfa_secret(user_id, mfa_secrets.encrypt_secret(secret), enabled=False)
    _audit("mfa.enroll.begin", user_id=user_id)
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, email=email)}


def confirm_enrollment(user_id: str, code: str) -> bool:
    """Enable MFA once *code* proves the pending secret was received."""
    account_row = _load_account_by_id(user_id)
    if not account_row:
        return False
    secret = mfa_secrets.decrypt_secret(account_row.get("mfa_secret_enc"))
    if secret is None or not totp.verify(secret, code):
        _audit("mfa.enroll.confirm", status="denied", user_id=user_id)
        return False
    _set_mfa_enabled(user_id, True)
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
    secret = _current_secret(account_row)
    if secret is None or not totp.verify(secret, code):
        _audit("mfa.disable", status="denied", user_id=user_id)
        return False
    _clear_mfa(user_id)
    _audit("mfa.disable", user_id=user_id)
    return True


def reset_mfa(user_id: str) -> None:
    """Administratively clear MFA (operator CLI recovery path)."""
    _clear_mfa(user_id)
    _audit("mfa.reset", user_id=user_id)
