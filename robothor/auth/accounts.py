"""DAL for ``user_accounts`` / ``user_sessions`` + JIT provisioning + owner bootstrap.

Follows the ``robothor.crm.dal`` pattern (``get_connection`` + ``RealDictCursor``
+ explicit commit). All queries are tenant-scoped where applicable, consistent
with the rest of the platform's multi-tenant isolation.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import Json, RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import (
    get_connection,
    read_every_tenant_in_transaction,
    unbound_scope,
)
from robothor.sanitize import sanitize_log

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


class AccountMoveError(RuntimeError):
    """``move_account`` refused before writing anything. The message says why."""


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


def owner_account_exists(tenant_id: str | None = None) -> bool:
    """Whether this instance has an owner account at all.

    The signal first-run setup is finished by. Deliberately a DATABASE
    question: a file-backed one would let anyone who can delete a file re-open
    the wizard on a running appliance and create the second owner.

    ``tenant_id=None`` asks across every tenant, which is the question the
    setup gate needs: an owner in ANY tenant means this box has been claimed,
    and scoping the check to the default tenant would leave a wizard open on an
    instance whose operator happens to live somewhere else.

    Raises whatever the connection raises. The caller decides what an
    unreachable database means, and for the setup gate it means "complete" —
    see :func:`robothor.setup_token.setup_complete`.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant_id is None:
            cur.execute(
                "SELECT 1 FROM user_accounts WHERE role = 'owner' AND status = 'active' LIMIT 1"
            )
        else:
            cur.execute(
                "SELECT 1 FROM user_accounts WHERE tenant_id = %s AND role = 'owner' "
                "AND status = 'active' LIMIT 1",
                (tenant_id,),
            )
        return cur.fetchone() is not None


# ── Administration (the Helm's Users & roles page, and `genus user`) ──
#
# Everything below projects an EXPLICIT column list. ``SELECT *`` is what the
# rest of this module uses, correctly — sign-in needs the password hash and the
# MFA secret — but these functions answer an administrator's browser, and a
# projection is the only lock that survives somebody adding a column later.

#: What an administration surface may see about an account. Notably absent, and
#: deliberately: ``password_hash``, ``mfa_secret_enc`` and ``idp_subject``. The
#: first two are credentials; the third is a third-party identity provider's own
#: id for a person, which is not this platform's to hand out. ``sso_bound`` is
#: computed instead, because "is this account attached to an IdP" is the whole
#: of what an operator needs.
_ADMIN_COLUMNS = (
    "id, tenant_id, email, display_name, role, status, person_id, "
    "mfa_enabled, last_login_at, created_at, updated_at, "
    "(idp_issuer IS NOT NULL AND idp_subject IS NOT NULL) AS sso_bound"
)

#: The columns :func:`update_account` may write, mapped to their SQL fragment.
#: A literal table rather than an f-string over caller-supplied names: this is
#: the one statement on the administration surface whose shape depends on the
#: request body.
_UPDATABLE = {
    "role": "role = %s",
    "display_name": "display_name = %s",
    "status": "status = %s",
}


def list_accounts(tenant_id: str) -> list[dict[str, Any]]:
    """Every account in one tenant, without any of their credentials."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {_ADMIN_COLUMNS} FROM user_accounts WHERE tenant_id = %s "  # noqa: S608
            "ORDER BY created_at ASC, id ASC",
            (tenant_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_admin_account(user_id: str, tenant_id: str) -> dict[str, Any] | None:
    """One account, scoped to a tenant.

    ``tenant_id`` is a required argument and not an option: an administration
    read that could be called without it would answer for any account on the
    appliance the moment one caller forgot, and "forgot" is what a
    cross-tenant enumeration looks like from the code's side.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {_ADMIN_COLUMNS} FROM user_accounts "  # noqa: S608
            "WHERE id = %s AND tenant_id = %s",
            (user_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def owner_count(
    tenant_id: str, *, excluding_id: str | None = None, active_only: bool = True
) -> int:
    """How many owners a tenant has, optionally ignoring one account.

    The question a demotion or a disable has to ask first. An appliance with no
    owner has nobody who can administer it and no supported way back in —
    ``bootstrap_owner_account`` runs from ``owner.yaml`` on the box, which is a
    shell an operator locked out of the Helm may not have.

    ``active_only=False`` counts owner ROWS whatever their status, and that is
    the question the administration API asks. An ``invited`` or ``disabled``
    owner still occupies migration 071's ``uq_user_accounts_owner`` slot — the
    partial unique index is on ``tenant_id WHERE role = 'owner'`` and knows
    nothing about status — so demoting one does not merely change a row, it
    FREES THE SLOT for whoever asks next. Counting only active owners is what
    let an admin take it (review round 1, C1), and ``invited`` is the state
    ``genus user add --role owner`` leaves behind on every CLI-built instance.
    """
    # A literal fragment chosen by a boolean, never a caller's string — the
    # same rule ``_UPDATABLE`` follows.
    status_clause = " AND status = 'active'" if active_only else ""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM user_accounts "  # noqa: S608
            f"WHERE tenant_id = %s AND role = 'owner'{status_clause} "
            "AND (%s::text IS NULL OR id <> %s::uuid)",
            (tenant_id, excluding_id, excluding_id),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def create_account(
    *,
    tenant_id: str,
    email: str,
    display_name: str,
    role: str,
    status: str,
    person_id: str | None = None,
) -> dict[str, Any] | None:
    """Create one account. ``None`` when the address is already taken.

    The address is canonicalised here rather than trusted from the caller, for
    the reason :func:`get_account_by_email` gives: a row written as
    ``Alice@Example.com`` is a row the sign-in path cannot find.

    ``None`` rather than an exception for the duplicate, because a duplicate is
    an ordinary answer to "invite this person" and the caller turns it into a
    409. Everything else — a role the column check refuses, a second owner —
    raises, and the caller must not flatten those into the same answer.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""INSERT INTO user_accounts
                    (tenant_id, email, display_name, role, status, person_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, email) DO NOTHING
                RETURNING {_ADMIN_COLUMNS}""",  # noqa: S608
            (tenant_id, canonical_email(email), display_name, role, status, person_id),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def update_account(
    user_id: str,
    *,
    tenant_id: str,
    role: str | None = None,
    display_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Change one account, scoped to a tenant. ``None`` if it is not there.

    The ``tenant_id`` predicate is in the UPDATE itself rather than in a read
    the caller did first: a check-then-write leaves a window, and the window on
    this statement is one where an account moves tenants between the two.
    """
    assignments: list[str] = []
    values: list[Any] = []
    for field, value in (("role", role), ("display_name", display_name), ("status", status)):
        if value is not None:
            assignments.append(_UPDATABLE[field])
            values.append(value)
    if not assignments:
        return get_admin_account(user_id, tenant_id)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""UPDATE user_accounts SET {", ".join(assignments)}, updated_at = NOW()
                WHERE id = %s AND tenant_id = %s
                RETURNING {_ADMIN_COLUMNS}""",  # noqa: S608
            (*values, user_id, tenant_id),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def get_account_by_idp(issuer: str, subject: str) -> dict[str, Any] | None:
    """The account bound to ``(issuer, subject)``, in whichever tenant it lives.

    Unbound (``robothor.db.connection.unbound_scope``): the pair is unique
    platform-wide, and a lookup bound to one tenant misses every other
    tenant's account — after which ``jit_provision`` INSERTs a duplicate and
    sign-in fails on a UniqueViolation.
    """
    with unbound_scope(), get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_accounts WHERE idp_issuer = %s AND idp_subject = %s",
            (issuer, subject),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def tenant_for_email(email: str) -> str:
    """The tenant a brand-new SSO user with this address is provisioned into.

    The tenant that claims the address's domain in ``settings.sso_domains``
    (:mod:`robothor.crm.tenant_domains`), else ``DEFAULT_TENANT``. Exact domain
    match only: claiming ``example.com`` does not claim ``mail.example.com``.

    Never raises. This runs on the sign-in path, so every unclear answer — no
    claim, two tenants claiming one domain (a configuration error the writer
    refuses, logged here as an error), an unreadable tenant table — lands on
    the default tenant rather than failing the login. Only inactive-tenant
    claims are ignored: a disabled tenant must not receive new accounts.
    """
    _local, at, domain = canonical_email(email).rpartition("@")
    if not at or not domain:
        return DEFAULT_TENANT
    try:
        from robothor.crm import dal
        from robothor.crm.tenant_domains import sso_domains_of

        tenants = dal.list_tenants(active_only=True)
    except Exception:
        logger.exception("tenant_for_email: tenant table unreadable; using the default tenant")
        return DEFAULT_TENANT
    matches = sorted({t["id"] for t in tenants if domain in sso_domains_of(t)})
    if len(matches) == 1:
        return str(matches[0])
    if matches:
        logger.error(
            "tenant_for_email: domain %s is claimed by %d tenants (%s); "
            "provisioning into the default tenant until the claim is fixed",
            sanitize_log(domain),
            len(matches),
            sanitize_log(", ".join(matches)),
        )
    return DEFAULT_TENANT


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

    # Unbound throughout: the bound account may live in a child tenant, and
    # the login stamp, grant consumption and bind must all see the row the
    # lookup found. The INSERT names its tenant explicitly.
    with unbound_scope():
        return _jit_provision(
            issuer=issuer,
            subject=subject,
            email=email,
            display_name=display_name,
            tenant_id=tenant_id,
            default_role=default_role,
        )


def _jit_provision(
    *,
    issuer: str,
    subject: str,
    email: str,
    display_name: str,
    tenant_id: str,
    default_role: str,
) -> dict[str, Any]:
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

    Unbound: a refresh carries no access token, so nothing says which tenant
    the session's account lives in until this has answered.
    """
    with unbound_scope(), get_connection() as conn:
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
        revoked = _revoke_sessions(conn.cursor(), user_id, except_refresh_hash)
        conn.commit()
        return revoked


def _revoke_sessions(cur: Any, user_id: str, except_refresh_hash: str | None = None) -> int:
    """Revoke one account's live refresh sessions in the caller's transaction."""
    if except_refresh_hash:
        cur.execute(
            "UPDATE user_sessions SET revoked_at = NOW() "
            "WHERE user_id = %s AND revoked_at IS NULL AND refresh_token_hash <> %s",
            (user_id, except_refresh_hash),
        )
    else:
        cur.execute(
            "UPDATE user_sessions SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
    return int(cur.rowcount or 0)


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


# ── Moving an account to another tenant ─────────────────────────────

#: Every table that holds a row linked to an account, in the order a move
#: rewrites them. ``user_sessions`` is not here: sessions are revoked, not moved.
MOVED_TABLES = (
    "user_accounts",
    "crm_people",
    "user_permissions",
    "user_channel_identities",
    "face_identities",
    "tenant_users",
    "sso_binding_grants",
)

#: Roles a move may assign. Never ``owner``: a move must not mint a second
#: owner, and a child tenant has none at all.
MOVE_ROLES = frozenset({"admin", "member", "viewer"})


@dataclass
class MoveReport:
    """What :func:`move_account` moved, or (``dry_run``) would move."""

    user_id: str
    email: str
    from_tenant: str
    to_tenant: str
    role_before: str
    role_after: str
    dry_run: bool
    counts: dict[str, int] = dataclass_field(default_factory=dict)
    sessions_revoked: int = 0
    audit_id: int | None = None
    #: Set by ``detach_person``: the fresh CRM contact made in the target.
    new_person_id: str | None = None
    #: The company the moved contact was linked to, when that company stays
    #: in another tenant: the link is cleared rather than left cross-tenant.
    company_unlinked: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Rows that reference ``crm_people`` but travel with the account, so are not
#: history: per table, a predicate that excludes them (``None`` excludes the
#: whole table). Every other row that references the person is its CRM history
#: — including another account linked to the same person, and a face
#: enrolment of the person that is not this account's.
_PERSON_REFS_MOVED_WITH_ACCOUNT: dict[str, str | None] = {
    "user_accounts": "id <> %(uid)s::uuid",
    "tenant_users": None,  # moved by person_id, every row
    "face_identities": "user_account_id IS DISTINCT FROM %(uid)s::uuid",
}


def _person_history(cur: Any, person_id: str, source: str, user_id: str) -> dict[str, int]:
    """Rows in the source tenant that reference the account's CRM contact.

    Found from the catalog, not a hand-kept list: every single-column foreign
    key onto ``crm_people`` in a table with a ``tenant_id``. A table added
    later is covered without anyone remembering this function.
    """
    from psycopg2 import sql

    cur.execute(
        """SELECT cl.relname AS tbl, a.attname AS col
           FROM pg_constraint c
           JOIN pg_class cl ON cl.oid = c.conrelid
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
           WHERE c.contype = 'f'
             AND c.confrelid = 'crm_people'::regclass
             AND cardinality(c.conkey) = 1
             AND EXISTS (SELECT 1 FROM pg_attribute t
                         WHERE t.attrelid = c.conrelid AND t.attname = 'tenant_id'
                           AND NOT t.attisdropped)
           ORDER BY cl.relname, a.attname"""
    )
    refs = [(r["tbl"], r["col"]) for r in cur.fetchall()]
    history: dict[str, int] = {}
    for table, column in refs:
        query = sql.SQL(
            "SELECT COUNT(*) AS n FROM {} WHERE {} = %(person)s AND tenant_id = %(source)s"
        ).format(sql.Identifier(table), sql.Identifier(column))
        if table in _PERSON_REFS_MOVED_WITH_ACCOUNT:
            exclusion = _PERSON_REFS_MOVED_WITH_ACCOUNT[table]
            if exclusion is None:
                continue
            # A literal from the table above, never caller input.
            query = query + sql.SQL(" AND " + exclusion)  # noqa: S608
        cur.execute(query, {"person": person_id, "source": source, "uid": user_id})
        n = int(cur.fetchone()["n"])
        if n:
            history[table] = history.get(table, 0) + n
    return history


def accounts_for_email(email: str) -> list[dict[str, Any]]:
    """Every account with this address, in any tenant, without credentials.

    The lookup ``genus tenant move-account --email`` needs: which tenant an
    address lives in is a question across tenants by definition, so the read
    widens RLS for its one transaction (pinned in
    ``test_benchmark_decontamination.py``).
    """
    with get_connection() as conn:
        read_every_tenant_in_transaction(conn)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {_ADMIN_COLUMNS} FROM user_accounts WHERE email = %s "  # noqa: S608
            "ORDER BY tenant_id",
            (canonical_email(email),),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.rollback()  # end the widened read-only transaction
        return rows


def _linked_rows(
    account: dict[str, Any], source: str, *, detach_person: bool = False
) -> list[tuple[str, str, tuple[Any, ...]]]:
    """``(table, WHERE clause, params)`` for every row group that moves with the account.

    Each predicate names the SOURCE tenant as well as the link, so a row the
    account is linked to in some other tenant is never dragged along. With
    ``detach_person`` the CRM contact stays behind.
    """
    uid = str(account["id"])
    person_id = account.get("person_id")
    groups: list[tuple[str, str, tuple[Any, ...]]] = [
        ("user_accounts", "id = %s", (uid,)),
    ]
    if person_id and not detach_person:
        groups.append(("crm_people", "id = %s AND tenant_id = %s", (str(person_id), source)))
    groups += [
        ("user_permissions", "tenant_id = %s AND user_id = %s", (source, uid)),
        ("user_channel_identities", "tenant_id = %s AND user_id = %s", (source, uid)),
        ("face_identities", "tenant_id = %s AND user_account_id = %s", (source, uid)),
    ]
    # tenant_users is Telegram's source of truth (lookup_user reads nothing
    # else). A pairing writes it keyed by the account's id with no person_id,
    # so select by either link: a row left behind would keep driving the
    # source tenant's agent.
    groups.append(
        (
            "tenant_users",
            "tenant_id = %s AND (user_id = %s OR person_id = %s::uuid)",
            (source, uid, str(person_id) if person_id else None),
        )
    )
    groups.append(
        (
            "sso_binding_grants",
            "tenant_id = %s AND email = %s AND used_at IS NULL AND revoked_at IS NULL",
            (source, str(account["email"])),
        )
    )
    return groups


def _refuse_move(
    cur: Any, account: dict[str, Any], user_id: str, to_tenant: str, role: str | None
) -> None:
    """Raise ``AccountMoveError`` for every reason the move must not happen."""
    source = account["tenant_id"]
    if role is not None and role not in MOVE_ROLES:
        raise AccountMoveError(
            f"a move can assign role {', '.join(sorted(MOVE_ROLES))}, not {role!r}"
        )
    cur.execute("SELECT id, active, parent_tenant_id FROM crm_tenants WHERE id = %s", (to_tenant,))
    target = cur.fetchone()
    if not target:
        raise AccountMoveError(f"target tenant {to_tenant!r} does not exist")
    if not target["active"]:
        raise AccountMoveError(f"target tenant {to_tenant!r} is not active")
    if source == to_tenant:
        raise AccountMoveError(f"account is already in tenant {to_tenant!r}")
    if account["role"] == "owner":
        # Any status counts: an invited or disabled owner still holds the
        # one-owner slot (migration 071), see ``owner_count``.
        cur.execute(
            "SELECT COUNT(*) AS n FROM user_accounts "
            "WHERE tenant_id = %s AND role = 'owner' AND id <> %s",
            (source, user_id),
        )
        if int(cur.fetchone()["n"]) == 0:
            raise AccountMoveError(
                f"account is the last owner of tenant {source!r}; make someone else its owner first"
            )
    if (role or account["role"]) == "owner" and target["parent_tenant_id"]:
        raise AccountMoveError(
            f"tenant {to_tenant!r} is a child tenant and has no owner; "
            "move the account with --role admin"
        )
    cur.execute(
        "SELECT id FROM user_accounts WHERE tenant_id = %s AND email = %s",
        (to_tenant, str(account["email"])),
    )
    if cur.fetchone():
        raise AccountMoveError(
            f"tenant {to_tenant!r} already has an account for {account['email']}"
        )


def _detach_person(cur: Any, old_person: str, to_tenant: str, user_id: str) -> str:
    """Give an already-moved account a fresh CRM contact in its new tenant.

    Copies the name and address from the old contact, which stays put. The
    account's Telegram routing row and face enrolment, already moved, are
    relinked to the new contact.
    """
    cur.execute(
        """INSERT INTO crm_people (tenant_id, first_name, last_name, email)
           SELECT %s, first_name, last_name, email FROM crm_people WHERE id = %s
           RETURNING id""",
        (to_tenant, old_person),
    )
    new_person = str(cur.fetchone()["id"])
    cur.execute("UPDATE user_accounts SET person_id = %s WHERE id = %s", (new_person, user_id))
    cur.execute(
        "UPDATE tenant_users SET person_id = %s WHERE tenant_id = %s AND person_id = %s",
        (new_person, to_tenant, old_person),
    )
    cur.execute(
        "UPDATE face_identities SET person_id = %s "
        "WHERE tenant_id = %s AND user_account_id = %s AND person_id = %s",
        (new_person, to_tenant, user_id, old_person),
    )
    return new_person


def move_account(
    user_id: str,
    to_tenant: str,
    *,
    actor: str,
    role: str | None = None,
    dry_run: bool = False,
    detach_person: bool = False,
) -> MoveReport:
    """Move one account, and every row linked to it, to another tenant.

    ONE transaction, under :func:`read_every_tenant_in_transaction` (the move
    reads the source tenant and writes the target; a connection bound to
    either would see half of it). In it: every refusal is checked before the
    first write; the row groups in :data:`MOVED_TABLES` are rewritten; the
    account's live refresh sessions are revoked, so the next token it gets
    carries the new tenant; and one ``audit_log`` row records the move. Any
    failure rolls all of it back.

    ``role`` reassigns the role on the way (one of :data:`MOVE_ROLES`);
    ``None`` keeps it. A demotion to ``viewer`` also caps the account's
    channel identities at ``viewer``; nothing ever raises them, because in a
    non-default tenant a ``member`` pairing grants every tool (migration 118).
    ``dry_run`` reports the per-table counts and rolls back.

    The account's CRM contact (``person_id``) moves with it only when nothing
    else in the source tenant references that contact. If notes, tasks,
    conversations or anything else do, the move is refused with the counts —
    moving the contact would leave that history pointing at a person the
    source tenant can no longer see. ``detach_person`` resolves it: the
    contact and its history stay in the source tenant, and the account (with
    its Telegram routing row and face enrolment) is relinked to a fresh
    contact created in the target from the old one's name and address.

    Raises ``AccountMoveError`` for a refusal; database errors propagate after
    the rollback.
    """
    with get_connection() as conn:
        try:
            read_every_tenant_in_transaction(conn)
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM user_accounts WHERE id = %s FOR UPDATE", (user_id,))
            account = cur.fetchone()
            if not account:
                raise AccountMoveError(f"no account with id {user_id!r}")
            _refuse_move(cur, account, user_id, to_tenant, role)
            source = account["tenant_id"]
            old_person = str(account["person_id"]) if account.get("person_id") else None
            detach = bool(old_person) and detach_person
            if old_person and not detach:
                history = _person_history(cur, old_person, source, str(account["id"]))
                if history:
                    listed = ", ".join(f"{t}={n}" for t, n in sorted(history.items()))
                    raise AccountMoveError(
                        f"the account's CRM contact has history in tenant {source!r} "
                        f"({listed}); pass --detach-person to leave the contact and its "
                        "history there and give the account a new contact in the target"
                    )
            report = MoveReport(
                user_id=str(account["id"]),
                email=str(account["email"]),
                from_tenant=source,
                to_tenant=to_tenant,
                role_before=account["role"],
                role_after=role or account["role"],
                dry_run=dry_run,
                counts=dict.fromkeys(MOVED_TABLES, 0),
            )
            groups = _linked_rows(account, source, detach_person=detach)
            if old_person and not detach:
                cur.execute(
                    "SELECT p.company_id FROM crm_people p "
                    "JOIN crm_companies c ON c.id = p.company_id "
                    "WHERE p.id = %s AND c.tenant_id IS DISTINCT FROM %s",
                    (old_person, to_tenant),
                )
                foreign = cur.fetchone()
                if foreign:
                    report.company_unlinked = str(foreign["company_id"])

            if dry_run:
                for table, where, params in groups:
                    cur.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE {where}",  # noqa: S608
                        params,
                    )
                    report.counts[table] = int(cur.fetchone()["n"])
                if detach:
                    report.counts["crm_people"] = 1  # the fresh contact it would create
                conn.rollback()
                return report

            cap_channel_role = role == "viewer"
            for table, where, params in groups:
                if table == "user_accounts":
                    cur.execute(
                        "UPDATE user_accounts SET tenant_id = %s, role = %s, "
                        "updated_at = NOW() WHERE id = %s",
                        (to_tenant, report.role_after, report.user_id),
                    )
                elif table in ("user_channel_identities", "tenant_users"):
                    # Both carry the channel's own role; a demotion caps it.
                    cur.execute(
                        f"UPDATE {table} SET tenant_id = %s, "  # noqa: S608
                        "role = CASE WHEN %s THEN 'viewer' ELSE role END "
                        f"WHERE {where}",
                        (to_tenant, cap_channel_role, *params),
                    )
                elif table == "crm_people":
                    # A company left in another tenant is unlinked, not dragged.
                    cur.execute(
                        "UPDATE crm_people SET tenant_id = %s, "
                        "company_id = CASE WHEN %s THEN NULL ELSE company_id END "
                        f"WHERE {where}",  # noqa: S608
                        (to_tenant, report.company_unlinked is not None, *params),
                    )
                else:
                    cur.execute(
                        f"UPDATE {table} SET tenant_id = %s WHERE {where}",  # noqa: S608
                        (to_tenant, *params),
                    )
                report.counts[table] = int(cur.rowcount or 0)

            if detach:
                report.new_person_id = _detach_person(
                    cur, old_person or "", to_tenant, report.user_id
                )
                report.counts["crm_people"] = 1

            report.sessions_revoked = _revoke_sessions(cur, report.user_id)

            details = {
                k: v for k, v in report.to_dict().items() if k not in ("audit_id", "dry_run")
            }
            cur.execute(
                """INSERT INTO audit_log
                       (event_type, category, actor, action, details, target, status, user_id)
                   VALUES ('auth.account_moved', 'auth', %s, %s, %s, %s, 'ok', %s)
                   RETURNING id""",
                (
                    actor,
                    f"move account {report.user_id} from {source} to {to_tenant}",
                    Json(details),
                    f"user_account:{report.user_id}",
                    report.user_id,
                ),
            )
            report.audit_id = int(cur.fetchone()["id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info(
        "move_account: %s moved %s from %s to %s (%s)",
        actor,
        report.user_id,
        report.from_tenant,
        report.to_tenant,
        report.counts,
    )
    return report


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
