"""DAL for ``user_accounts`` / ``user_sessions`` + JIT provisioning + owner bootstrap.

Follows the ``robothor.crm.dal`` pattern (``get_connection`` + ``RealDictCursor``
+ explicit commit). All queries are tenant-scoped where applicable, consistent
with the rest of the platform's multi-tenant isolation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

DEFAULT_ROLE = "member"
JIT_PROVISIONABLE_ROLES = frozenset({"member", "viewer"})


def canonical_email(email: str | None) -> str:
    """The one canonical form of an address, used on every read and write.

    ``str.lower()``, NOT ``str.casefold()``. Casefold is for caseless
    *comparison* of arbitrary text and maps ß to ss, so
    ``"Straße@example.com".casefold()`` is ``"strasse@example.com"`` — a different mailbox,
    quite possibly someone else's. ``lower()`` matches PostgreSQL's ``lower()``
    and CITEXT's comparison exactly, which is what the stored side and the
    migration use, so the Python and SQL sides can never disagree about which
    row an address names.
    """
    return (email or "").strip().lower()


class AccountProvisioningError(RuntimeError):
    """Base class for fail-closed SSO account resolution failures."""


class AccountInactiveError(AccountProvisioningError):
    """The IdP identity is bound to an account that is not active."""


class AccountBindingRequiredError(AccountProvisioningError):
    """An existing email identity needs an explicit administrator binding."""


class UnsafeProvisioningRoleError(AccountProvisioningError):
    """JIT provisioning attempted to create a privileged account."""


class GrantTargetError(RuntimeError):
    """A binding grant was armed for an account that could never consume it."""


def get_account_by_id(user_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_accounts WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_account_by_email(tenant_id: str, email: str) -> dict[str, Any] | None:
    """Look up one account by address, case-insensitively.

    The address is canonicalised HERE rather than trusted from the caller, so
    every path — sign-in, the CLI, the bridge — asks the same question. The
    stored side is canonical too (migration 114 lower-cased what existed;
    ``bootstrap_owner_account`` and ``genus user add`` casefold on write), so
    this stays an equality predicate and keeps using the
    ``UNIQUE (tenant_id, email)`` index.

    ``canonical_email`` is ``lower()``, never ``casefold()`` — see its note.

    It is deliberately NOT ``lower(email) = lower(%s)``. That would work, but
    it makes the predicate non-sargable — a sequential scan of every account
    on the busiest unauthenticated route in the product, which is a denial-of-
    service lever handed to anyone who can POST /api/auth/login. Canonical on
    both sides gives the same case-insensitivity with the index intact, and on
    this schema the column is CITEXT as well, so the guarantee holds twice.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_accounts WHERE tenant_id = %s AND email = %s",
            (tenant_id, canonical_email(email)),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_account_by_idp(issuer: str, subject: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_accounts WHERE idp_issuer = %s AND idp_subject = %s",
            (issuer, subject),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def jit_provision(
    *,
    issuer: str,
    subject: str,
    email: str,
    display_name: str,
    tenant_id: str = DEFAULT_TENANT,
    default_role: str = DEFAULT_ROLE,
) -> dict[str, Any]:
    """Resolve (or create) the account for a verified SSO identity, just-in-time.

    Resolution order: (1) an active account already bound to ``(issuer,
    subject)``; (2) a new, non-privileged account when the email is unused.

    Email equality is deliberately *not* an identity-binding mechanism.  An
    existing account (including the owner bootstrap account and invitations)
    must be explicitly bound to the IdP subject by an administrator before it
    can sign in.  This prevents a verified email claim from silently taking
    over a pre-existing or privileged local account.
    """
    if default_role not in JIT_PROVISIONABLE_ROLES:
        raise UnsafeProvisioningRoleError("privileged roles cannot be JIT provisioned")

    email = canonical_email(email)
    existing = get_account_by_idp(issuer, subject)
    if existing:
        if existing.get("status") != "active":
            raise AccountInactiveError("SSO account is not active")
        return _touch_login(existing["id"])

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Never turn a matching email claim into an identity binding.  The
        # explicit safe bindings are a pre-populated (idp_issuer, idp_subject)
        # pair (resolved by the branch above) or an operator-armed one-shot
        # grant consumed atomically below.
        cur.execute(
            "SELECT * FROM user_accounts WHERE tenant_id = %s AND email = %s",
            (tenant_id, email),
        )
        row = cur.fetchone()
        if row:
            # Inactive accounts fail before the grant lookup (the grant is not
            # burned), and a grant never re-binds an already-bound account.
            if row.get("status") != "active" or row.get("idp_issuer"):
                raise AccountBindingRequiredError(
                    "existing account must be explicitly bound to the SSO identity"
                )
            grant = _consume_binding_grant(cur, tenant_id, email, issuer)
            if not grant:
                # A concurrent sign-in from this same identity may have just
                # consumed the grant and bound the account — that is a win,
                # not an error, for the user standing at the login page.
                won = _rebound_by_same_identity(cur, issuer, subject)
                if won:
                    return won
                raise AccountBindingRequiredError(
                    "existing account must be explicitly bound to the SSO identity"
                )
            cur.execute(
                """UPDATE user_accounts
                   SET idp_issuer = %s, idp_subject = %s,
                       last_login_at = NOW(), updated_at = NOW()
                   WHERE id = %s AND status = 'active' AND idp_issuer IS NULL
                   RETURNING *""",
                (issuer, subject, row["id"]),
            )
            bound = cur.fetchone()
            if not bound:
                won = _rebound_by_same_identity(cur, issuer, subject)
                if won:
                    return won
                # Raced with a different bind; roll back so the grant survives.
                raise AccountBindingRequiredError(
                    "existing account must be explicitly bound to the SSO identity"
                )
            cur.execute(
                "UPDATE sso_binding_grants SET used_by_issuer = %s, used_by_subject = %s "
                "WHERE id = %s",
                (issuer, subject, grant["id"]),
            )
            conn.commit()
            logger.info(
                "jit_provision: binding grant %s consumed; account %s bound to issuer %s",
                grant["id"],
                bound["id"],
                issuer,
            )
            return dict(bound)

        cur.execute(
            """INSERT INTO user_accounts
                   (tenant_id, email, idp_issuer, idp_subject, display_name,
                    role, status, last_login_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW())
               RETURNING *""",
            (tenant_id, email, issuer, subject, display_name or email, default_role),
        )
        out = cur.fetchone()
        conn.commit()
        return dict(out)


def _touch_login(user_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "UPDATE user_accounts SET last_login_at = NOW(), updated_at = NOW() "
            "WHERE id = %s AND status = 'active' RETURNING *",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AccountInactiveError("SSO account is not active")
        conn.commit()
        return dict(row)


# ── Binding grants (explicit one-shot account↔IdP binding) ──────────


def create_binding_grant(
    *,
    tenant_id: str = DEFAULT_TENANT,
    email: str,
    ttl_seconds: int,
    reason: str = "",
    created_by: str = "cli",
    issuer: str | None = None,
) -> dict[str, Any]:
    """Arm a one-shot grant allowing the next verified SSO login with this
    email to bind its (issuer, subject) onto the existing account.

    Fails loudly (GrantTargetError) when no active, unbound account exists for
    the email — a grant that can never fire is an operator lockout waiting to
    be diagnosed at sign-in time. Re-arming revokes any still-pending grant for
    the same email so at most one grant is ever live. An ``issuer`` pin
    restricts consumption to that IdP; otherwise any allowlisted IdP matches.
    """
    email = canonical_email(email)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_accounts WHERE tenant_id = %s AND email = %s",
            (tenant_id, email),
        )
        target = cur.fetchone()
        if not target:
            raise GrantTargetError(f"no account with email {email!r} in tenant {tenant_id!r}")
        if target.get("idp_issuer"):
            raise GrantTargetError("account is already bound to an SSO identity")
        if target.get("status") != "active":
            raise GrantTargetError("account is not active")
        cur.execute(
            "UPDATE sso_binding_grants SET revoked_at = NOW() "
            "WHERE tenant_id = %s AND email = %s AND used_at IS NULL AND revoked_at IS NULL",
            (tenant_id, email),
        )
        cur.execute(
            """INSERT INTO sso_binding_grants
                   (tenant_id, email, reason, created_by, expires_at, issuer)
               VALUES (%s, %s, %s, %s, NOW() + make_interval(secs => %s), %s)
               RETURNING *""",
            (tenant_id, email, reason, created_by, ttl_seconds, issuer),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)


def list_binding_grants(
    tenant_id: str = DEFAULT_TENANT, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        state = """CASE
                       WHEN revoked_at IS NOT NULL THEN 'revoked'
                       WHEN used_at IS NOT NULL THEN 'used'
                       WHEN expires_at <= NOW() THEN 'expired'
                       ELSE 'pending'
                   END AS state"""
        if include_inactive:
            cur.execute(
                f"SELECT *, {state} FROM sso_binding_grants WHERE tenant_id = %s "
                "ORDER BY created_at DESC",
                (tenant_id,),
            )
        else:
            cur.execute(
                f"""SELECT *, {state} FROM sso_binding_grants
                   WHERE tenant_id = %s
                     AND used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW()
                   ORDER BY created_at DESC""",
                (tenant_id,),
            )
        return [dict(r) for r in cur.fetchall()]


def revoke_binding_grant(grant_id: str, tenant_id: str | None = None) -> bool:
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant_id is None:
            cur.execute(
                "UPDATE sso_binding_grants SET revoked_at = NOW() "
                "WHERE id = %s AND revoked_at IS NULL AND used_at IS NULL",
                (grant_id,),
            )
        else:
            cur.execute(
                "UPDATE sso_binding_grants SET revoked_at = NOW() "
                "WHERE id = %s AND tenant_id = %s AND revoked_at IS NULL AND used_at IS NULL",
                (grant_id, tenant_id),
            )
        revoked = bool(cur.rowcount > 0)
        conn.commit()
        return revoked


def _consume_binding_grant(
    cur: Any, tenant_id: str, email: str, presenting_issuer: str
) -> dict[str, Any] | None:
    """Atomically spend one live grant (single UPDATE ... RETURNING, mirroring
    ``consume_active_session``): two concurrent sign-ins must not both bind.
    Issuer-pinned grants are only spendable by that IdP."""
    cur.execute(
        """UPDATE sso_binding_grants
           SET used_at = NOW()
           WHERE id = (
               SELECT id FROM sso_binding_grants
               WHERE tenant_id = %s AND email = %s
                 AND used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW()
                 AND (issuer IS NULL OR issuer = %s)
               ORDER BY created_at DESC
               LIMIT 1
               FOR UPDATE SKIP LOCKED
           )
           RETURNING *""",
        (tenant_id, email, presenting_issuer),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _rebound_by_same_identity(cur: Any, issuer: str, subject: str) -> dict[str, Any] | None:
    """After losing a bind race, check whether the account is now bound to
    exactly the presenting identity — if so the sign-in should succeed."""
    cur.execute(
        "SELECT * FROM user_accounts "
        "WHERE idp_issuer = %s AND idp_subject = %s AND status = 'active'",
        (issuer, subject),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ── Sessions (refresh-token revocation) ─────────────────────────────


def create_session(
    user_id: str,
    refresh_token_hash: str,
    *,
    ttl_seconds: int,
    user_agent: str | None = None,
    ip: str | None = None,
) -> str:
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO user_sessions
                   (user_id, refresh_token_hash, expires_at, user_agent, ip)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (user_id, refresh_token_hash, expires_at, user_agent, ip),
        )
        sid = cur.fetchone()["id"]
        conn.commit()
        return str(sid)


def get_active_session(refresh_token_hash: str) -> dict[str, Any] | None:
    """Return the session row if it exists, is unrevoked, and unexpired."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT * FROM user_sessions
               WHERE refresh_token_hash = %s
                 AND revoked_at IS NULL AND expires_at > NOW()""",
            (refresh_token_hash,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def consume_active_session(refresh_token_hash: str) -> dict[str, Any] | None:
    """Atomically revoke and return one valid refresh session.

    Rotation must be a single database operation: a separate read followed by
    a revoke allows two concurrent requests to reuse the same refresh token.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """UPDATE user_sessions
               SET revoked_at = NOW()
               WHERE refresh_token_hash = %s
                 AND revoked_at IS NULL AND expires_at > NOW()
               RETURNING *""",
            (refresh_token_hash,),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def issue_for_account(
    account: dict[str, Any], *, user_agent: str | None = None, ip: str | None = None
) -> dict[str, Any]:
    """Mint the access + refresh pair for an already-authenticated account.

    The single issuance point for every sign-in path — SSO exchange, refresh
    rotation, and local email+password. It lives here rather than in the
    bridge router so that a second path cannot quietly grow a second set of
    rules about what a session is: the active-status check, the refresh-token
    hashing, and the exact user payload are all decided once.

    Raises ``AccountInactiveError`` for anything but an active account; a
    caller must translate that into its own generic failure, never echo it.
    """
    from robothor.auth import tokens

    if account.get("status") != "active":
        raise AccountInactiveError("account is not active")
    access = tokens.issue_access_token(str(account["id"]), account["tenant_id"], account["role"])
    raw_refresh, refresh_hash = tokens.new_refresh_token()
    create_session(
        str(account["id"]),
        refresh_hash,
        ttl_seconds=tokens.REFRESH_TTL_SECONDS,
        user_agent=user_agent,
        ip=ip,
    )
    return {
        "access_token": access,
        "refresh_token": raw_refresh,
        "user": {
            "id": str(account["id"]),
            "email": str(account["email"]),
            "display_name": account["display_name"],
            "role": account["role"],
            "tenant_id": account["tenant_id"],
        },
    }


def revoke_user_sessions(user_id: str, *, except_refresh_hash: str | None = None) -> int:
    """Revoke live refresh sessions for one account. Returns how many.

    Called whenever the account's password changes. A password change that
    leaves a stolen refresh token working has not locked anyone out — the
    thief simply rotates it every thirty days and the owner never finds out.

    ``except_refresh_hash`` spares the caller's OWN session, so changing a
    password does not eject the operator from the page they are standing on.
    Sparing it is not a weakening: that session just proved it holds the old
    password, which is exactly what the revocation is testing for.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if except_refresh_hash:
            cur.execute(
                "UPDATE user_sessions SET revoked_at = NOW() "
                "WHERE user_id = %s AND revoked_at IS NULL AND refresh_token_hash <> %s",
                (user_id, except_refresh_hash),
            )
        else:
            cur.execute(
                "UPDATE user_sessions SET revoked_at = NOW() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
        revoked = int(cur.rowcount or 0)
        conn.commit()
        return revoked


def revoke_session(refresh_token_hash: str) -> bool:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_sessions SET revoked_at = NOW() "
            "WHERE refresh_token_hash = %s AND revoked_at IS NULL",
            (refresh_token_hash,),
        )
        revoked = bool(cur.rowcount > 0)
        conn.commit()
        return revoked


# ── Owner bootstrap (single-operator → first admin) ─────────────────


def bootstrap_owner_account() -> dict[str, Any] | None:
    """Seed the ``owner.yaml`` operator as the tenant's ``owner`` user account.

    Idempotent. Returns the account, or None if no operator is configured. The
    account has no credentials yet. Its exact trusted OIDC issuer + subject
    must be bound out-of-band before SSO; a verified email never auto-links a
    privileged account. Break-glass access is also provisioned out-of-band.
    """
    from robothor.owner_config import load_owner_config

    owner = load_owner_config()
    if not owner or not owner.email:
        logger.info("bootstrap_owner_account: no operator configured; skipping")
        return None

    tenant_id = owner.tenant_id or DEFAULT_TENANT
    display = " ".join(p for p in (owner.first_name, owner.last_name) if p) or owner.email
    # Canonical, lower-case storage. Sign-in casefolds what the browser sent,
    # so an owner seeded from an owner.yaml reading "Alice@Example.com" must
    # not be stored in a form the lookup can miss. The column happens to be
    # CITEXT today, which would have hidden this — but a guarantee that rests
    # on one column type is not a guarantee, and the owner row is the one
    # account whose lockout has no recovery path.
    email = canonical_email(owner.email)

    # Link the CRM rolodex row if resolvable (mirrors tenant_users.person_id).
    person_id = None
    try:
        from robothor.crm.dal import get_owner_person

        person = get_owner_person(tenant_id)
        person_id = person.get("id") if person else None
    except Exception:
        logger.debug("bootstrap_owner_account: owner person link unavailable", exc_info=True)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO user_accounts (tenant_id, email, display_name, role, status, person_id)
               VALUES (%s, %s, %s, 'owner', 'active', %s)
               ON CONFLICT (tenant_id, email) DO UPDATE
                   SET role = 'owner', person_id = COALESCE(EXCLUDED.person_id, user_accounts.person_id),
                       updated_at = NOW()
               RETURNING *""",
            (tenant_id, email, display, person_id),
        )
        row = cur.fetchone()
        conn.commit()
        logger.info("bootstrap_owner_account: owner '%s' seeded for tenant '%s'", email, tenant_id)
        return dict(row)
