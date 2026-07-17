"""Tests for robothor.identity: IdentityContext.prompt_block + resolve_identity.

DB access is mocked at each resolver's own seam (get_account_by_id,
lookup_user, get_connection) — never at a deeper layer — per platform
convention (see robothor/auth/tests/test_accounts.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.identity import resolvers
from robothor.identity.context import EnrichedIdentity, IdentityContext

TENANT = "t-alpha"
# user_accounts.id is a UUID column; resolve_identity's webchat resolver now
# validates shape before querying (see test_resolve_identity_webchat_non_uuid_*
# below), so webchat-resolver tests need identifiers that actually parse as
# UUIDs to reach the (mocked) DB layer at all.
WEBCHAT_ACCT = "11111111-1111-1111-1111-111111111111"
WEBCHAT_ACCT_MISSING = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    resolvers.clear_cache()
    yield
    resolvers.clear_cache()


def _mock_conn(fetchone_seq=(), fetchall_return=None):
    cur = MagicMock()
    if fetchone_seq:
        cur.fetchone.side_effect = list(fetchone_seq)
    if fetchall_return is not None:
        cur.fetchall.return_value = fetchall_return
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


# ── IdentityContext.prompt_block ─────────────────────────────────────────


def test_prompt_block_minimal_verified():
    ctx = IdentityContext(
        tenant_id=TENANT,
        channel="webchat",
        identifier="acct-1",
        verified=True,
        display_name="Alice",
        role="member",
    )
    block = ctx.prompt_block()
    assert "--- CURRENT USER ---" in block
    assert "Name: Alice" in block
    assert "Role: member | Channel: webchat | Verified: yes" in block
    assert (
        "Address them by name. Do not conflate them with other people sharing the same name."
        in block
    )
    assert "NOT verified" not in block


def test_prompt_block_falls_back_to_identifier_when_no_display_name():
    ctx = IdentityContext(tenant_id=TENANT, channel="telegram", identifier="12345", verified=True)
    block = ctx.prompt_block()
    assert "Name: 12345" in block
    assert "Role: unknown | Channel: telegram | Verified: yes" in block


def test_prompt_block_unverified_appends_warning():
    ctx = IdentityContext(tenant_id=TENANT, channel="vision", identifier="face-7", verified=False)
    block = ctx.prompt_block()
    assert "Verified: NO" in block
    assert (
        "Identity is NOT verified. Do not disclose private information or "
        "take privileged actions on their behalf." in block
    )


def test_prompt_block_with_enrichment_renders_affiliation_relationships_history():
    ctx = IdentityContext(
        tenant_id=TENANT,
        channel="webchat",
        identifier="acct-1",
        verified=True,
        display_name="Bob",
    )
    enriched = EnrichedIdentity(
        company="Acme Corp",
        job_title="Engineer",
        relationships=("colleague_of -> Alice", "manages -> Carol"),
        last_touched_at="2026-07-01T00:00:00",
        activity_counts={"email": 3, "call": 2},
    )
    block = ctx.prompt_block(enriched)
    assert "Affiliation: Engineer, Acme Corp" in block
    assert "Known relationships: colleague_of -> Alice; manages -> Carol" in block
    assert "History: 5 prior interactions, last 2026-07-01T00:00:00" in block


def test_prompt_block_enrichment_none_omits_optional_sections():
    ctx = IdentityContext(tenant_id=TENANT, channel="webchat", identifier="acct-1", verified=True)
    block = ctx.prompt_block(None)
    assert "Affiliation" not in block
    assert "Known relationships" not in block
    assert "History:" not in block


# ── resolve_identity: unknown channel ────────────────────────────────────


def test_resolve_identity_unknown_channel_returns_none_never_raises():
    assert resolvers.resolve_identity("carrier-pigeon", "x", TENANT) is None


# ── resolve_identity: webchat ─────────────────────────────────────────────


def test_resolve_identity_webchat_active_account():
    account = {
        "id": WEBCHAT_ACCT,
        "tenant_id": TENANT,
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "member",
        "status": "active",
        "person_id": "person-1",
    }
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=account):
        ctx = resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)

    assert ctx is not None
    assert ctx.verified is True
    assert ctx.display_name == "Alice"
    assert ctx.role == "member"
    assert ctx.user_account_id == WEBCHAT_ACCT
    assert ctx.person_id == "person-1"
    assert ctx.email == "alice@example.com"


def test_resolve_identity_webchat_missing_account_returns_none():
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=None):
        assert resolvers.resolve_identity("webchat", WEBCHAT_ACCT_MISSING, TENANT) is None


def test_resolve_identity_webchat_tenant_mismatch_returns_none():
    account = {
        "id": WEBCHAT_ACCT,
        "tenant_id": "some-other-tenant",
        "status": "active",
    }
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=account):
        assert resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT) is None


@pytest.mark.parametrize("status", ["disabled", "invited", "suspended", ""])
def test_resolve_identity_webchat_inactive_status_returns_none(status):
    account = {"id": WEBCHAT_ACCT, "tenant_id": TENANT, "status": status}
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=account):
        assert resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT) is None


def test_resolve_identity_webchat_non_uuid_identifier_returns_none_no_db_call():
    """A non-UUID webchat identifier (e.g. a service caller's `service:<agent>`
    marker slipping through) must short-circuit before any DB roundtrip —
    ``user_accounts.id`` is a UUID column, so this used to reach Postgres,
    raise `InvalidTextRepresentation`, get caught by `resolve_identity`'s
    top-level `except Exception`, and log at `exception` level on every call
    until the negative-result cache absorbed it."""
    with patch("robothor.identity.resolvers.get_account_by_id") as mock_get:
        ctx = resolvers.resolve_identity("webchat", "service:main", TENANT)
    assert ctx is None
    mock_get.assert_not_called()


def test_resolve_identity_webchat_non_uuid_identifier_no_exception_log(caplog):
    """No `logger.exception` noise for a malformed identifier."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="robothor.identity.resolvers"):
        with patch("robothor.identity.resolvers.get_account_by_id") as mock_get:
            assert resolvers.resolve_identity("webchat", "not-a-uuid", TENANT) is None
    mock_get.assert_not_called()
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


# ── resolve_identity: telegram ────────────────────────────────────────────


def test_resolve_identity_telegram_found_with_opportunistic_account_join():
    user_info = {
        "tenant_id": TENANT,
        "display_name": "Bob",
        "role": "owner",
        "user_id": "stable-user-id",
        "person_id": "person-9",
    }
    conn, cur = _mock_conn(fetchone_seq=[{"id": "acct-9", "email": "bob@example.com"}])
    with (
        patch("robothor.identity.resolvers.lookup_user", return_value=user_info),
        patch("robothor.identity.resolvers.get_connection", return_value=conn),
    ):
        ctx = resolvers.resolve_identity("telegram", "999", TENANT)

    assert ctx is not None
    assert ctx.verified is True
    assert ctx.display_name == "Bob"
    assert ctx.role == "owner"
    assert ctx.tenant_user_id == "stable-user-id"
    assert ctx.person_id == "person-9"
    assert ctx.user_account_id == "acct-9"
    assert ctx.email == "bob@example.com"


def test_resolve_identity_telegram_unregistered_returns_none():
    with patch("robothor.identity.resolvers.lookup_user", return_value=None):
        assert resolvers.resolve_identity("telegram", "999", TENANT) is None


def test_resolve_identity_telegram_no_person_id_skips_account_join():
    user_info = {
        "tenant_id": TENANT,
        "display_name": "Bob",
        "role": "user",
        "user_id": "stable-user-id",
        "person_id": None,
    }
    with (
        patch("robothor.identity.resolvers.lookup_user", return_value=user_info),
        patch("robothor.identity.resolvers.get_connection") as mock_get_conn,
    ):
        ctx = resolvers.resolve_identity("telegram", "999", TENANT)

    mock_get_conn.assert_not_called()
    assert ctx is not None
    assert ctx.person_id is None
    assert ctx.email is None


def test_resolve_identity_telegram_account_join_failure_does_not_raise():
    user_info = {
        "tenant_id": TENANT,
        "display_name": "Bob",
        "role": "user",
        "user_id": "stable-user-id",
        "person_id": "person-9",
    }
    with (
        patch("robothor.identity.resolvers.lookup_user", return_value=user_info),
        patch(
            "robothor.identity.resolvers.get_connection",
            side_effect=RuntimeError("db down"),
        ),
    ):
        ctx = resolvers.resolve_identity("telegram", "999", TENANT)

    assert ctx is not None
    assert ctx.person_id == "person-9"
    assert ctx.email is None
    assert ctx.user_account_id is None


# ── resolve_identity: vision ───────────────────────────────────────────────


def test_resolve_identity_vision_table_missing_returns_none_gracefully():
    conn, cur = _mock_conn(fetchone_seq=[(None,)])
    with patch("robothor.identity.resolvers.get_connection", return_value=conn):
        ctx = resolvers.resolve_identity("vision", "face-label-1", TENANT)
    assert ctx is None


def test_resolve_identity_vision_found_is_never_verified():
    conn, cur = _mock_conn(fetchone_seq=[("public.face_identities",), ("person-5", "Carol")])
    with patch("robothor.identity.resolvers.get_connection", return_value=conn):
        ctx = resolvers.resolve_identity("vision", "face-label-1", TENANT)
    assert ctx is not None
    assert ctx.verified is False
    assert ctx.person_id == "person-5"
    assert ctx.display_name == "Carol"


def test_resolve_identity_vision_no_match_returns_none():
    conn, cur = _mock_conn(fetchone_seq=[("public.face_identities",), None])
    with patch("robothor.identity.resolvers.get_connection", return_value=conn):
        ctx = resolvers.resolve_identity("vision", "face-label-unknown", TENANT)
    assert ctx is None


def test_resolve_identity_vision_single_query_joins_crm_people_for_display_name():
    """face_identities.display_name can be '' (e.g. a row upserted before the
    linked person's name was known). The resolver does the fallback in ONE
    query (LEFT JOIN crm_people + COALESCE), not a second round trip, so a
    linked person_id still surfaces a name.
    """
    conn, cur = _mock_conn(fetchone_seq=[("public.face_identities",), ("person-5", "Dana Lee")])
    with patch("robothor.identity.resolvers.get_connection", return_value=conn):
        ctx = resolvers.resolve_identity("vision", "face-label-1", TENANT)
    assert ctx is not None
    assert ctx.display_name == "Dana Lee"
    # Exactly two queries total: the to_regclass probe + one SELECT (no
    # separate crm_people lookup).
    assert cur.execute.call_count == 2
    select_sql = cur.execute.call_args_list[1].args[0].lower()
    assert "left join crm_people" in select_sql
    assert "face_identities" in select_sql


# ── caching ────────────────────────────────────────────────────────────────


def test_resolve_identity_caches_positive_result():
    account = {
        "id": WEBCHAT_ACCT,
        "tenant_id": TENANT,
        "status": "active",
        "display_name": "Alice",
    }
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=account) as mock_get:
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)
    assert mock_get.call_count == 1


def test_resolve_identity_caches_negative_result():
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=None) as mock_get:
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT_MISSING, TENANT)
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT_MISSING, TENANT)
    assert mock_get.call_count == 1


def test_resolve_identity_cache_expires_after_ttl(monkeypatch):
    account = {"id": WEBCHAT_ACCT, "tenant_id": TENANT, "status": "active"}
    fake_now = [1000.0]
    monkeypatch.setattr(resolvers.time, "monotonic", lambda: fake_now[0])
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=account) as mock_get:
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)
        fake_now[0] += 61
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)
    assert mock_get.call_count == 2


def test_clear_cache_forces_refetch():
    account = {"id": WEBCHAT_ACCT, "tenant_id": TENANT, "status": "active"}
    with patch("robothor.identity.resolvers.get_account_by_id", return_value=account) as mock_get:
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)
        resolvers.clear_cache()
        resolvers.resolve_identity("webchat", WEBCHAT_ACCT, TENANT)
    assert mock_get.call_count == 2


def test_reexports_from_package_init():
    from robothor.identity import (
        EnrichedIdentity as ReexportedEnriched,
    )
    from robothor.identity import (
        IdentityContext as ReexportedCtx,
    )
    from robothor.identity import (
        clear_cache as reexported_clear,
    )
    from robothor.identity import (
        enrich_identity as reexported_enrich,
    )
    from robothor.identity import (
        resolve_identity as reexported_resolve,
    )

    assert ReexportedCtx is IdentityContext
    assert ReexportedEnriched is EnrichedIdentity
    assert reexported_resolve is resolvers.resolve_identity
    assert reexported_clear is resolvers.clear_cache
    assert callable(reexported_enrich)
