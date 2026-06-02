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


def get_account_by_id(user_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_accounts WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_account_by_email(tenant_id: str, email: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_accounts WHERE tenant_id = %s AND email = %s",
            (tenant_id, email),
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

    Resolution order: (1) existing (issuer, subject); (2) existing (tenant, email)
    with the IdP identity linked onto it; (3) create a new account at
    ``default_role``. Stamps ``last_login_at``. The IdP did the authentication;
    this only maps the verified identity to a local account + role.
    """
    existing = get_account_by_idp(issuer, subject)
    if existing:
        return _touch_login(existing["id"])

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Link IdP identity onto a pre-existing email account (e.g. break-glass
        # admin, or an invited user) if present.
        cur.execute(
            "SELECT * FROM user_accounts WHERE tenant_id = %s AND email = %s",
            (tenant_id, email),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """UPDATE user_accounts
                   SET idp_issuer = %s, idp_subject = %s,
                       display_name = COALESCE(NULLIF(%s, ''), display_name),
                       last_login_at = NOW(), updated_at = NOW()
                   WHERE id = %s
                   RETURNING *""",
                (issuer, subject, display_name, row["id"]),
            )
        else:
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
            "WHERE id = %s RETURNING *",
            (user_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)


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


def revoke_session(refresh_token_hash: str) -> bool:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_sessions SET revoked_at = NOW() "
            "WHERE refresh_token_hash = %s AND revoked_at IS NULL",
            (refresh_token_hash,),
        )
        revoked = cur.rowcount > 0
        conn.commit()
        return revoked


# ── Owner bootstrap (single-operator → first admin) ─────────────────


def bootstrap_owner_account() -> dict[str, Any] | None:
    """Seed the ``owner.yaml`` operator as the tenant's ``owner`` user account.

    Idempotent. Returns the account, or None if no operator is configured. The
    account has no credentials yet (SSO links on first login; break-glass
    password set out-of-band) — it just establishes the owner identity + role so
    enforcement never locks the operator out.
    """
    from robothor.owner_config import load_owner_config

    owner = load_owner_config()
    if not owner or not owner.email:
        logger.info("bootstrap_owner_account: no operator configured; skipping")
        return None

    tenant_id = owner.tenant_id or DEFAULT_TENANT
    display = " ".join(p for p in (owner.first_name, owner.last_name) if p) or owner.email

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
            (tenant_id, owner.email, display, person_id),
        )
        row = cur.fetchone()
        conn.commit()
        logger.info(
            "bootstrap_owner_account: owner '%s' seeded for tenant '%s'", owner.email, tenant_id
        )
        return dict(row)
