"""Pairing: the one invariant, and the SQL that has to carry it.

**A channel message may never approve a pairing.** Everything else here exists
to make that statement checkable. The attack it stops is the shortest one in
the book: the stranger who was just handed a code sends ``approve ABC234`` back
down the same wire, and something on the inbound path — a command handler, an
LLM that decided to be helpful, a tool — spends it.

So approval takes a mandatory ``actor`` and refuses any value that did not come
from an operator-gated bridge route (``operator:``) or the operator's own shell
(``cli:``). The inbound path has no code that can construct one, and the test
below feeds the plausible shapes back through the gate to prove it.

The rest is the grant machinery ``robothor/auth/accounts.py`` already settled
for SSO binding grants, applied to a channel: single use enforced by ``UPDATE
... WHERE used_at IS NULL ... RETURNING`` rather than by a Python check, a
short TTL, and a code that is stored as a sha256 and never as itself.

The DB-backed half is ``integration``-marked and runs against a scratch
database migrated through 118. The invariant itself is not: it must hold on a
box with no database at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from robothor.engine.channels import access, identities

SLACK_USER = "U0PLACEHOLDER"
TELEGRAM_USER = "100000001"
TENANT = "default"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES

    for name in list(os.environ):
        if (name.startswith("ROBOTHOR_") and name.endswith("_ACCESS")) or (
            name in DEPRECATED_ALIASES
        ):
            monkeypatch.delenv(name, raising=False)
    reset_settings()
    access.reset_reply_budget()
    identities.reset_code_memo()
    yield
    reset_settings()
    access.reset_reply_budget()
    identities.reset_code_memo()


# ── The invariant, with no database in sight ─────────────────────────────────


@pytest.mark.parametrize(
    "actor",
    [
        "telegram:100000001",
        "slack:U0PLACEHOLDER",
        "agent:main",
        "service",
        "",
        "operator",  # no colon: the prefix check is not a substring check
        "cli",
        "xoperator:1",
    ],
)
def test_only_an_operator_or_the_cli_may_approve(actor):
    with pytest.raises(identities.PairingActorError):
        identities.approve_pairing("ABC234", actor=actor, user_id="u-1", tenant_id=TENANT)


def test_approval_actor_is_mandatory_and_keyword_only():
    import inspect

    sig = inspect.signature(identities.approve_pairing)
    actor = sig.parameters["actor"]
    assert actor.kind is inspect.Parameter.KEYWORD_ONLY
    assert actor.default is inspect.Parameter.empty


def test_a_channel_message_can_never_approve_a_pairing(monkeypatch):
    """Feed the plausible shapes back in through the inbound gate.

    ``evaluate`` is the whole of what an inbound message reaches. Whatever the
    text is, the only thing it can produce is another refusal — there is no
    argument, and no message content, that reaches ``approve_pairing``.
    """
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: None)
    monkeypatch.setattr(access.identities, "mint_code", lambda **kw: "ABC234")

    approvals: list[Any] = []
    monkeypatch.setattr(
        access.identities, "approve_pairing", lambda *a, **kw: approvals.append((a, kw))
    )

    for text in ("approve ABC234", "/approve ABC234", "ABC234"):
        decision = asyncio.run(
            access.evaluate(
                channel="slack",
                native_id=SLACK_USER,
                tenant_id=TENANT,
                display_name=text,
            )
        )
        assert decision.allowed is False

    assert approvals == []


def test_a_code_is_stored_as_a_hash_and_never_as_itself():
    code = identities.generate_code()

    assert identities.code_hash(code) != code
    assert len(identities.code_hash(code)) == 64


def test_generated_codes_avoid_the_ambiguous_characters():
    for _ in range(200):
        code = identities.generate_code()
        assert len(code) == identities.PAIRING_CODE_LENGTH
        assert not (set(code) & set("IO01"))


def test_a_privileged_role_is_never_pairable():
    for role in ("owner", "admin"):
        with pytest.raises(identities.PairingRoleError):
            identities.approve_pairing(
                "ABC234", actor="cli:tester", user_id="u-1", role=role, tenant_id=TENANT
            )


def test_approval_needs_exactly_one_of_user_or_email():
    with pytest.raises(identities.PairingTargetError):
        identities.approve_pairing("ABC234", actor="cli:tester", tenant_id=TENANT)
    with pytest.raises(identities.PairingTargetError):
        identities.approve_pairing(
            "ABC234",
            actor="cli:tester",
            user_id="u-1",
            email="alice@example.com",
            tenant_id=TENANT,
        )


def test_no_native_ids_or_display_names_in_logs(monkeypatch, caplog):
    """The line this gate replaces logged the raw Slack user id on every
    refusal (``engine/slack.py``), which put a workspace's member ids into
    every log shipper the instance has."""
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    monkeypatch.setattr(access, "_resolve_known", lambda *a, **kw: None)
    monkeypatch.setattr(access.identities, "mint_code", lambda **kw: "ABC234")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            access.evaluate(
                channel="slack",
                native_id=SLACK_USER,
                tenant_id=TENANT,
                display_name="Alice Example",
            )
        )
        asyncio.run(
            access.evaluate(
                channel="slack",
                native_id=SLACK_USER,
                tenant_id=TENANT,
                display_name="Alice Example",
                surface=access.GROUP_SURFACE,
            )
        )

    emitted = " | ".join(record.getMessage() for record in caplog.records)
    assert SLACK_USER not in emitted
    assert "Alice Example" not in emitted
    assert "ABC234" not in emitted


# ── The DB-backed half ───────────────────────────────────────────────────────


@pytest.fixture
def paired_db(scratch_db, monkeypatch):
    """A scratch database at migration 118, wired under the DAL."""
    from robothor.engine import users as engine_users
    from robothor.identity import resolvers

    db, dsn = scratch_db(through="118_user_channel_identities")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (OTHER_TENANT, "Other"),
        )

    @contextlib.contextmanager
    def _conn():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    for module in (identities, resolvers, engine_users):
        monkeypatch.setattr(module, "get_connection", _conn)
    resolvers.clear_cache()
    engine_users.clear_cache()
    return db


def _live_codes(db, channel: str = "slack") -> list[dict[str, Any]]:
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM channel_pairing_codes WHERE channel = %s ORDER BY created_at", (channel,)
    )
    return [dict(r) for r in cur.fetchall()]


def _identity_rows(db) -> list[dict[str, Any]]:
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM user_channel_identities ORDER BY paired_at")
    return [dict(r) for r in cur.fetchall()]


@pytest.mark.integration
def test_minting_twice_keeps_one_live_row_and_one_live_code(paired_db):
    first = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)
    second = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    assert first == second
    rows = _live_codes(paired_db)
    assert len(rows) == 1
    assert rows[0]["code_hash"] == identities.code_hash(first)
    assert rows[0]["code_hash"] != first


@pytest.mark.integration
def test_the_plaintext_code_is_nowhere_in_the_row(paired_db):
    code = identities.mint_code(
        channel="slack", native_id=SLACK_USER, tenant_id=TENANT, display_name="Alice Example"
    )

    row = _live_codes(paired_db)[0]
    assert code not in " ".join(str(v) for v in row.values())


@pytest.mark.integration
def test_a_code_is_single_use(paired_db):
    """Two approvals of the same code: exactly one gets a RETURNING row."""
    code = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    identities.approve_pairing(
        code, actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-alice"
    )
    with pytest.raises(identities.PairingCodeError):
        identities.approve_pairing(
            code, actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-bob"
        )

    assert len(_identity_rows(paired_db)) == 1


@pytest.mark.integration
def test_code_expires_after_ten_minutes(paired_db):
    code = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)
    with paired_db.cursor() as cur:
        cur.execute("UPDATE channel_pairing_codes SET expires_at = NOW() - interval '1 second'")

    with pytest.raises(identities.PairingCodeError):
        identities.approve_pairing(
            code, actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-alice"
        )

    assert _identity_rows(paired_db) == []
    assert _live_codes(paired_db)[0]["used_at"] is None


@pytest.mark.integration
def test_the_ttl_is_ten_minutes(paired_db):
    identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    row = _live_codes(paired_db)[0]
    window = (row["expires_at"] - row["created_at"]).total_seconds()
    assert 595 <= window <= 605


@pytest.mark.integration
def test_a_denied_code_cannot_be_approved(paired_db):
    code = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    identities.deny_pairing(code, actor="operator:admin-1", channel="slack", tenant_id=TENANT)

    with pytest.raises(identities.PairingCodeError):
        identities.approve_pairing(
            code, actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-alice"
        )
    assert _identity_rows(paired_db) == []


@pytest.mark.integration
def test_denial_only_accepts_an_operator_actor(paired_db):
    code = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    with pytest.raises(identities.PairingActorError):
        identities.deny_pairing(code, actor="slack:U0PLACEHOLDER", tenant_id=TENANT)
    assert _live_codes(paired_db)[0]["denied_at"] is None


@pytest.mark.integration
def test_approval_binds_the_identity_and_the_next_message_runs_as_that_user(paired_db, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SLACK_ACCESS", "pairing")
    code = identities.mint_code(
        channel="slack", native_id=SLACK_USER, tenant_id=TENANT, display_name="Alice Example"
    )

    bound = identities.approve_pairing(
        code, actor="operator:admin-1", channel="slack", tenant_id=TENANT, user_id="u-alice"
    )

    assert bound["user_id"] == "u-alice"
    assert bound["paired_by"] == "operator:admin-1"
    found = identities.lookup("slack", SLACK_USER, tenant_id=TENANT)
    assert found is not None
    assert found["user_id"] == "u-alice"

    decision = asyncio.run(access.evaluate(channel="slack", native_id=SLACK_USER, tenant_id=TENANT))
    assert decision.allowed is True
    assert decision.identity is not None
    assert decision.identity.identifier == SLACK_USER
    assert decision.identity.role == "member"


@pytest.mark.integration
def test_revoked_identity_can_re_pair(paired_db):
    code = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)
    bound = identities.approve_pairing(
        code, actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-alice"
    )

    assert identities.revoke(bound["id"], tenant_id=TENANT, actor="cli:tester") is True
    assert identities.lookup("slack", SLACK_USER, tenant_id=TENANT) is None

    again = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)
    rebound = identities.approve_pairing(
        again, actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-alice"
    )

    assert rebound["id"] != bound["id"]
    assert len(_identity_rows(paired_db)) == 2


@pytest.mark.integration
def test_a_telegram_approval_also_writes_the_tenant_users_row(paired_db):
    """``lookup_user`` stays the source of truth for Telegram, so a pairing
    that only wrote the mirror row would bind somebody the inbound path still
    treats as a stranger."""
    code = identities.mint_code(
        channel="telegram", native_id=TELEGRAM_USER, tenant_id=TENANT, display_name="Alice Example"
    )

    identities.approve_pairing(
        code, actor="cli:tester", channel="telegram", tenant_id=TENANT, user_id="u-alice"
    )

    cur = paired_db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM tenant_users WHERE telegram_user_id = %s AND tenant_id = %s",
        (TELEGRAM_USER, TENANT),
    )
    row = cur.fetchone()
    assert row is not None
    assert row["is_active"] is True
    assert row["role"] == "member"


@pytest.mark.integration
def test_listing_pending_codes_never_returns_the_code_or_the_native_id(paired_db):
    identities.mint_code(
        channel="slack", native_id=SLACK_USER, tenant_id=TENANT, display_name="Alice Example"
    )

    pending = identities.list_pending("slack", tenant_id=TENANT)

    assert len(pending) == 1
    rendered = " ".join(f"{k}={v}" for k, v in pending[0].items())
    assert SLACK_USER not in rendered
    assert "code_hash" not in pending[0]
    assert pending[0]["display_name_present"] is True


@pytest.mark.integration
def test_an_unknown_code_is_refused_rather_than_silently_ignored(paired_db):
    with pytest.raises(identities.PairingCodeError):
        identities.approve_pairing(
            "ZZZZZZ", actor="cli:tester", channel="slack", tenant_id=TENANT, user_id="u-alice"
        )


@pytest.mark.integration
def test_a_code_minted_in_one_tenant_is_not_approvable_in_another(paired_db):
    code = identities.mint_code(channel="slack", native_id=SLACK_USER, tenant_id=TENANT)

    with pytest.raises(identities.PairingCodeError):
        identities.approve_pairing(
            code, actor="cli:tester", channel="slack", tenant_id=OTHER_TENANT, user_id="u-alice"
        )
