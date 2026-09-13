"""Channel identities and the one-shot codes that create them.

Two rows and one rule. The rows are ``user_channel_identities`` (this native id
belongs to this user) and ``channel_pairing_codes`` (a short-lived grant that
can create one); migration 118 explains their shape at length. The rule is:

    **A channel message may never approve a pairing.**

It is enforced here rather than at the call sites, because call sites multiply.
:func:`approve_pairing` and :func:`deny_pairing` take a mandatory keyword
``actor`` and raise :exc:`PairingActorError` for anything that did not come from
an operator-gated bridge route (``operator:``) or the operator's own shell
(``cli:``). The inbound path holds no value either prefix will accept, so the
stranger who sends ``approve ABC234`` back down the wire that gave them the code
is refused by construction and not by a check somebody has to remember.

**Sync, by design.** Like :mod:`robothor.engine.agent_questions`, this is a
psycopg2 DAL shared by the engine, the bridge and the CLI, and two of those are
synchronous. The engine's one caller is
:mod:`robothor.engine.channels.access`, which is async and reaches every
function here through ``asyncio.to_thread`` — so no blocking call runs on the
event loop, and there is not a second async copy of this API to keep in step.

Never stores a code. ``code_hash`` is a sha256 hex digest, the same treatment
``user_sessions.refresh_token_hash`` gives a refresh token: for the ten minutes
it lives a pairing code IS a credential, and a database dump must not carry a
spendable one.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "PAIRABLE_ROLES",
    "PAIRING_CODE_ALPHABET",
    "PAIRING_CODE_LENGTH",
    "PAIRING_TTL_SECONDS",
    "PairingActorError",
    "PairingCodeError",
    "PairingConflictError",
    "PairingDeniedError",
    "PairingError",
    "PairingRoleError",
    "PairingTargetError",
    "approve_pairing",
    "code_hash",
    "deny_pairing",
    "fingerprint",
    "generate_code",
    "list_identities",
    "list_pending",
    "lookup",
    "mint_code",
    "reset_code_memo",
    "revoke",
]

#: No I, O, 0 or 1. A code is read off a screen and typed into a terminal by
#: somebody who did not choose it, and the characters this alphabet omits are
#: the ones that turn a refused approval into a support conversation.
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 6

#: Ten minutes. Long enough to walk to the operator, short enough that a code
#: in a screenshot is dead by the time the screenshot is anywhere interesting.
PAIRING_TTL_SECONDS = 600

#: Mirrors ``accounts.JIT_PROVISIONABLE_ROLES``. A privileged role is never
#: granted by a flow whose first step is "a stranger sent a message".
PAIRABLE_ROLES = frozenset({"member", "viewer"})

#: The two callers that may settle a pairing, by the prefix their actor string
#: carries. ``operator:`` is minted by ``crm/bridge/routers/_operator.py``
#: AFTER its four-condition gate; ``cli:`` by a process already running as a
#: user with a shell on the box.
_APPROVAL_ACTOR_PREFIXES = ("operator:", "cli:")

#: How many senders each process-global map remembers.
#:
#: Both are keyed by native id and pruned only when a key is touched again, so
#: a flood of distinct senders would otherwise grow them without limit. The cap
#: is deliberately generous -- it is a backstop against unbounded growth, not a
#: capacity plan -- and eviction drops the oldest entries, which for the memo
#: means a sender gets a rotated code and for the reply budget means a fresh
#: allowance. Both are the same outcomes a restart produces.
_MAX_TRACKED_SENDERS = 5000

#: ``(tenant, channel, native_id) -> (code, monotonic deadline)``.
#:
#: The row stores a hash, so the plaintext cannot be read back out of it — and
#: a sender who retries inside the TTL must get the code they were already
#: given rather than watch it rotate under them. This remembers it for exactly
#: as long as the row is live, and only in the process that minted it: after a
#: restart the memo is empty and :func:`mint_code` rotates the pending row's
#: hash instead, which keeps the invariant that ONE live code exists per sender.
_code_memo: dict[tuple[str, str, str], tuple[str, float]] = {}


class PairingError(RuntimeError):
    """Base class for a pairing that was refused."""


class PairingActorError(PairingError):
    """The caller is not one a pairing may be settled by."""


class PairingRoleError(PairingError):
    """The role asked for is not one pairing may grant."""


class PairingTargetError(PairingError):
    """The approval named no user, or named two."""


class PairingCodeError(PairingError):
    """No live code matched: unknown, expired, already spent, or denied."""


class PairingConflictError(PairingError):
    """The binding collides with one that already exists.

    Separate from :exc:`PairingTargetError` because the operator's next move is
    different: a target error means they named the wrong person, a conflict
    means they named a person who is already bound and something has to be
    revoked first. It is a 409 at the router, never a 500 -- which is what an
    unhandled ``UniqueViolation`` was.
    """


class PairingDeniedError(PairingError):
    """This sender was refused and the refusal has not expired yet."""


# ── codes ────────────────────────────────────────────────────────────────────


def generate_code() -> str:
    """A fresh pairing code. ``secrets``, not ``random``: this is a credential."""
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def code_hash(code: str) -> str:
    """The only form of a code that is ever written down."""
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def fingerprint(native_id: str) -> str:
    """Enough to tell two senders apart, and nothing of either.

    An operator reviewing bindings needs to distinguish rows, which is a
    different need from reading a workspace's member ids -- and a listing that
    satisfied the second would let one leaked operator session enumerate every
    bound account on the channel. Same construction as
    ``robothor/cli/channel.py``'s credential fingerprint, and for the same
    reason: a prefix of the value itself would carry part of the value.
    """
    return hashlib.sha256((native_id or "").encode("utf-8")).hexdigest()[:12]


def reset_code_memo() -> None:
    """Forget every remembered plaintext. A test seam, and what a restart does."""
    _code_memo.clear()


def prune_oldest(tracked: dict[Any, Any], limit: int = _MAX_TRACKED_SENDERS) -> int:
    """Drop the oldest entries until ``tracked`` is within ``limit``.

    Python dicts preserve insertion order, so "oldest" is the front of the
    mapping. Shared with :mod:`robothor.engine.channels.access`, which has the
    same unbounded-growth problem in its reply budget and no reason to solve it
    differently. Returns how many were dropped, so a caller can say so.
    """
    dropped = 0
    while len(tracked) > limit:
        tracked.pop(next(iter(tracked)))
        dropped += 1
    return dropped


def _require_settling_actor(actor: str) -> str:
    """The actor, or :exc:`PairingActorError`.

    A prefix alone is not enough. ``crm/bridge/routers/_operator.py`` builds
    ``f"operator:{auth.actor_id}"`` unconditionally, so a null actor id would
    otherwise land in ``paired_by`` as the literal ``"operator:None"`` -- an
    audit row naming nobody, on the one table that records who let a stranger
    in. ``None`` is rejected by name for that reason and not on general
    principle.
    """
    actor = (actor or "").strip()
    matched = next((p for p in _APPROVAL_ACTOR_PREFIXES if actor.startswith(p)), "")
    identifier = actor[len(matched) :].strip() if matched else ""
    if not matched or not identifier or identifier == "None":
        # The actor is NOT logged. It is the one field an inbound caller would
        # control if this check ever regressed, and a refused value in a log is
        # a refused value in a log shipper.
        raise PairingActorError(
            "a pairing may only be settled by an identified operator-gated bridge "
            "route or the local CLI; a channel message can never approve one"
        )
    return actor


def mint_code(
    *,
    channel: str,
    native_id: str,
    tenant_id: str = DEFAULT_TENANT,
    display_name: str = "",
) -> str:
    """Return the live code for this sender, minting one if there is none.

    Idempotent inside the TTL: the partial unique index on ``(tenant_id,
    channel, native_id) WHERE used_at IS NULL AND denied_at IS NULL`` makes at
    most one live row possible, and the memo hands back the plaintext this
    process already sent. On a memo miss the pending row's hash is ROTATED
    rather than a second row inserted — one live grant per sender either way.
    """
    if _denial_is_live(tenant_id, channel, native_id):
        raise PairingDeniedError("this sender was refused and the refusal has not expired yet")

    key = (tenant_id, channel, native_id)
    remembered = _code_memo.get(key)
    if remembered is not None:
        code, deadline = remembered
        if time.monotonic() < deadline and _live_code_matches(tenant_id, channel, code):
            return code
        del _code_memo[key]

    code = generate_code()
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO channel_pairing_codes
                   (tenant_id, channel, native_id, code_hash, display_name, expires_at)
               VALUES (%s, %s, %s, %s, %s, NOW() + make_interval(secs => %s))
               ON CONFLICT (tenant_id, channel, native_id)
                   WHERE used_at IS NULL AND denied_at IS NULL
               DO UPDATE SET code_hash = EXCLUDED.code_hash,
                             expires_at = EXCLUDED.expires_at,
                             display_name = EXCLUDED.display_name
               RETURNING id""",
            (
                tenant_id,
                channel,
                native_id,
                code_hash(code),
                display_name,
                PAIRING_TTL_SECONDS,
            ),
        )
        cur.fetchone()
        conn.commit()

    _code_memo[key] = (code, time.monotonic() + PAIRING_TTL_SECONDS)
    prune_oldest(_code_memo)
    logger.info("channel %s minted a pairing code for tenant %s", channel, tenant_id)
    return code


def _denial_is_live(tenant_id: str, channel: str, native_id: str) -> bool:
    """Whether this sender was refused recently enough for it still to bind.

    Without this, ``deny`` barred one CODE rather than one SENDER: the next
    message minted a fresh row and the operator's pending list refilled, which
    makes a denial something a stranger can undo by typing again. The cooldown
    is the denied code's own remaining TTL -- ten minutes at most -- because a
    denial is "not now", not a ban, and a permanent block is a different
    decision with a different verb.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM channel_pairing_codes "
            "WHERE tenant_id = %s AND channel = %s AND native_id = %s "
            "AND denied_at IS NOT NULL AND expires_at > NOW() LIMIT 1",
            (tenant_id, channel, native_id),
        )
        return cur.fetchone() is not None


def _live_code_matches(tenant_id: str, channel: str, code: str) -> bool:
    """Whether the remembered plaintext still names a spendable row."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM channel_pairing_codes "
            "WHERE tenant_id = %s AND channel = %s AND code_hash = %s "
            "AND used_at IS NULL AND denied_at IS NULL AND expires_at > NOW()",
            (tenant_id, channel, code_hash(code)),
        )
        return cur.fetchone() is not None


def list_pending(channel: str, *, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
    """Pending codes, with neither the code nor the native id in the result.

    An operator deciding whether to approve needs to know that *somebody* is
    waiting and when their code dies. The native id would let a leaked operator
    session enumerate a workspace's members, and the code — even hashed — is
    the one column that must never travel.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, channel, expires_at, created_at, display_name <> '' AS display_name_present "
            "FROM channel_pairing_codes "
            "WHERE tenant_id = %s AND channel = %s "
            "AND used_at IS NULL AND denied_at IS NULL AND expires_at > NOW() "
            "ORDER BY created_at DESC LIMIT 200",
            (tenant_id, channel),
        )
        return [dict(row) for row in cur.fetchall()]


def _spend_code(
    cur: Any, code: str, tenant_id: str, channel: str, column: str
) -> dict[str, Any] | None:
    """Atomically settle one live code, mirroring ``_consume_binding_grant``.

    The ``RETURNING`` row IS the authorization. Two concurrent approvals of the
    same code both run this statement; the ``used_at IS NULL`` predicate and
    ``FOR UPDATE SKIP LOCKED`` mean exactly one of them gets a row back, and the
    other is refused by the database rather than by a Python check that read the
    row a moment before somebody else spent it.
    """
    channel_clause = "AND channel = %s" if channel else ""
    params: list[Any] = [tenant_id, code_hash(code)]
    if channel:
        params.append(channel)
    cur.execute(
        f"""UPDATE channel_pairing_codes
            SET {column} = NOW()
            WHERE id = (
                SELECT id FROM channel_pairing_codes
                WHERE tenant_id = %s AND code_hash = %s {channel_clause}
                  AND used_at IS NULL AND denied_at IS NULL AND expires_at > NOW()
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *""",
        tuple(params),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ── identities ───────────────────────────────────────────────────────────────


def lookup(
    channel: str, native_id: str, *, tenant_id: str = DEFAULT_TENANT
) -> dict[str, Any] | None:
    """The live binding for a native id, or None."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_channel_identities "
            "WHERE tenant_id = %s AND channel = %s AND native_id = %s AND revoked_at IS NULL",
            (tenant_id, channel, native_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _insert_identity(
    cur: Any,
    *,
    tenant_id: str,
    user_id: str,
    channel: str,
    native_id: str,
    display_name: str,
    role: str,
    paired_by: str,
) -> dict[str, Any]:
    """Write the binding, on the caller's cursor.

    **There is deliberately no public ``bind()``.** A function that wrote an
    identity row without spending a code would be a second way to grant access
    -- one with no TTL, no single-use guarantee and no evidence that the person
    on the other end ever asked. This takes a cursor precisely so it cannot be
    called on its own: the only caller is :func:`approve_pairing`, inside the
    transaction that just spent the grant, so the binding and the spend commit
    together or not at all.
    """
    cur.execute(
        """INSERT INTO user_channel_identities
               (tenant_id, user_id, channel, native_id, display_name, role, paired_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           RETURNING *""",
        (tenant_id, user_id, channel, native_id, display_name, role, paired_by),
    )
    return dict(cur.fetchone())


def list_identities(channel: str, *, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
    """Every live binding on a channel, for the operator who has to review them."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, channel, user_id, native_id, display_name, role, paired_at, paired_by "
            "FROM user_channel_identities "
            "WHERE tenant_id = %s AND channel = %s AND revoked_at IS NULL "
            "ORDER BY paired_at DESC LIMIT 500",
            (tenant_id, channel),
        )
        return [dict(row) for row in cur.fetchall()]


def revoke(identity_id: str, *, tenant_id: str = DEFAULT_TENANT, actor: str) -> bool:
    """Soft-delete a binding. Returns False when there was nothing live to revoke.

    A soft delete, so the record of who granted it survives the ungranting —
    and because the live-row uniqueness is partial, the same native id can pair
    again afterwards.
    """
    _require_settling_actor(actor)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_channel_identities SET revoked_at = NOW() "
            "WHERE id = %s AND tenant_id = %s AND revoked_at IS NULL",
            (identity_id, tenant_id),
        )
        revoked = bool(cur.rowcount > 0)
        conn.commit()
    if revoked:
        _forget_caches()
        logger.info("channel identity revoked in tenant %s", tenant_id)
    return revoked


# ── settling a code ──────────────────────────────────────────────────────────


def approve_pairing(
    code: str,
    *,
    actor: str,
    channel: str = "",
    tenant_id: str = DEFAULT_TENANT,
    user_id: str | None = None,
    email: str | None = None,
    role: str = "member",
) -> dict[str, Any]:
    """Spend a live code and bind its sender to ``user_id``.

    Raises before touching the database when the caller, the role or the target
    is wrong — an approval that was never going to be legitimate must not leave
    a spent code behind, because a spent code is a sender who can no longer
    retry.
    """
    _require_settling_actor(actor)
    if role not in PAIRABLE_ROLES:
        raise PairingRoleError(
            f"role {role!r} is not one pairing may grant "
            f"(allowed: {', '.join(sorted(PAIRABLE_ROLES))})"
        )
    if bool(user_id) == bool(email):
        raise PairingTargetError("name exactly one of a user id or an email address")

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        grant = _spend_code(cur, code, tenant_id, channel, "used_at")
        if grant is None:
            conn.rollback()
            raise PairingCodeError("no live code matched: unknown, expired, spent or denied")

        resolved = user_id or _user_id_for_email(cur, email or "", tenant_id)
        if not resolved:
            conn.rollback()
            raise PairingTargetError("no account in this tenant has that email address")

        try:
            if grant["channel"] == "telegram":
                _upsert_tenant_user(cur, grant, tenant_id, resolved, role)
            identity = _insert_identity(
                cur,
                tenant_id=tenant_id,
                user_id=resolved,
                channel=grant["channel"],
                native_id=grant["native_id"],
                display_name=grant.get("display_name") or "",
                role=role,
                paired_by=actor,
            )
        except psycopg2.IntegrityError as exc:
            # The spend and the binding share one transaction, so this rolls
            # BOTH back: the code stays live and the operator can approve it
            # again against a different target. A refused approval that burned
            # the grant would leave the sender unable to retry and the operator
            # with nothing left to approve.
            conn.rollback()
            raise PairingConflictError(_conflict_message(exc)) from exc
        conn.commit()

    _code_memo.pop((tenant_id, grant["channel"], grant["native_id"]), None)
    _forget_caches()
    logger.info("channel %s pairing approved in tenant %s", grant["channel"], tenant_id)
    return identity


def deny_pairing(
    code: str,
    *,
    actor: str,
    channel: str = "",
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Spend a live code WITHOUT binding anything.

    Denial goes through the same one-shot statement as approval rather than a
    delete, so a denied code can never be approved afterwards by a caller
    holding the same value — the row is still there and it is no longer live.
    """
    _require_settling_actor(actor)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        denied = _spend_code(cur, code, tenant_id, channel, "denied_at")
        if denied is None:
            conn.rollback()
            raise PairingCodeError("no live code matched: unknown, expired, spent or denied")
        conn.commit()

    _code_memo.pop((tenant_id, denied["channel"], denied["native_id"]), None)
    logger.info("channel %s pairing denied in tenant %s", denied["channel"], tenant_id)
    return {"id": str(denied["id"]), "channel": denied["channel"]}


def _user_id_for_email(cur: Any, email: str, tenant_id: str) -> str:
    """Resolve an email to an EXISTING account id. Never creates one.

    An approval that provisioned an account would make a stranger's message the
    first step in creating a user, which is the self-service onboarding this
    platform deliberately closed.
    """
    from robothor.auth.accounts import canonical_email

    cur.execute(
        "SELECT id::TEXT AS id FROM user_accounts "
        "WHERE tenant_id = %s AND email = %s AND status = 'active'",
        (tenant_id, canonical_email(email)),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else ""


def _conflict_message(exc: psycopg2.IntegrityError) -> str:
    """What the operator has to do about a collision, in their terms.

    The constraint names are the platform's, not theirs, and "duplicate key
    value violates unique constraint" is not an instruction.
    """
    detail = str(getattr(exc, "pgerror", "") or exc)
    if "tenant_users_user_id_key" in detail:
        return (
            "that user already has a Telegram binding; revoke it before pairing "
            "them to a different Telegram account"
        )
    if "uq_user_channel_identities_live" in detail:
        return "that sender is already bound on this channel; revoke the binding first"
    return "the binding collides with one that already exists"


def _upsert_tenant_user(
    cur: Any, grant: dict[str, Any], tenant_id: str, user_id: str, role: str
) -> None:
    """Telegram's source of truth is still ``tenant_users``.

    ``lookup_user`` reads that table and nothing else, and the whole Telegram
    inbound ladder is built on it. A pairing that wrote only the mirror row in
    ``user_channel_identities`` would bind somebody the inbound path still
    treats as a stranger — an approval with no effect, which is the failure
    mode this repo has shipped more than once.

    **``user_id`` moves with the binding.** The first cut set everything on the
    conflict branch EXCEPT that column, so re-pairing a native id to a new
    person left every Telegram run attributed to the old one — and with it the
    old ``person_id``, memory scoping and CRM linkage — while
    ``user_channel_identities`` said the new one. Two tables disagreeing
    permanently, with only the one nobody reads being right.

    Reactivating a deactivated row is deliberate: an operator approving a
    pairing IS the decision to let that person in, and refusing would make them
    guess which of two commands to run. But somebody deactivated that row on
    purpose, so it is never silent — one WARNING, carrying a count and no ids.
    """
    cur.execute(
        "SELECT is_active, user_id FROM tenant_users "
        "WHERE telegram_user_id = %s AND tenant_id = %s",
        (grant["native_id"], tenant_id),
    )
    existing = cur.fetchone()
    if existing is not None and not existing["is_active"]:
        logger.warning(
            "channel telegram pairing reactivated %d deactivated tenant_users row(s) "
            "in tenant %s; the approval is the decision to re-admit them",
            1,
            tenant_id,
        )

    cur.execute(
        """INSERT INTO tenant_users
               (telegram_user_id, display_name, tenant_id, role, user_id, is_active)
           VALUES (%s, %s, %s, %s, %s, TRUE)
           ON CONFLICT (telegram_user_id, tenant_id)
           DO UPDATE SET is_active = TRUE,
                         role = EXCLUDED.role,
                         user_id = EXCLUDED.user_id,
                         display_name = CASE
                             WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name
                             ELSE tenant_users.display_name
                         END,
                         updated_at = NOW()""",
        (
            grant["native_id"],
            grant.get("display_name") or "",
            tenant_id,
            role,
            user_id,
        ),
    )


def _forget_caches() -> None:
    """Drop the identity caches a binding change just invalidated.

    ``resolvers`` caches negatives for 60s and ``users`` for 60s too, so an
    approved sender would go on being refused for a minute after the operator
    watched the approval succeed — the kind of gap that gets diagnosed as "the
    approval did not work" and retried.
    """
    from robothor.engine.users import clear_cache as clear_user_cache
    from robothor.identity.resolvers import clear_cache as clear_identity_cache

    clear_identity_cache()
    clear_user_cache()
