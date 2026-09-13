"""Migration 118's two tables must be isolated by Postgres, not by a WHERE.

``tests/test_tenant_rls.py`` states the general case: the bridge's read helpers
take a bare id and no tenant, so a forgotten predicate is a cross-tenant read.
These two tables make that worse than usual — ``user_channel_identities`` is the
row that says *this person may drive the agent*, and ``channel_pairing_codes``
is the row that grants it. A leak here is not a disclosure, it is an
authorization.

The policy shape is copied verbatim from migration 106: permissive when
``app.tenant_id`` is unbound, so migrations, ``psql`` and the CLI keep working,
and confining when it is bound. All three halves are tested, because a policy
that only ever runs unbound is a policy that has never been exercised.
"""

from __future__ import annotations

import psycopg2
import pytest

pytestmark = pytest.mark.integration

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
CHANNEL = "slack"


@pytest.fixture
def channel_db(scratch_db):
    """A scratch database at 118 with one identity and one code per tenant."""
    db, dsn = scratch_db(through="118_user_channel_identities")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s), (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (TENANT_A, "A", TENANT_B, "B"),
        )
        for tenant, native in ((TENANT_A, "U0AAAAAAAAA"), (TENANT_B, "U0BBBBBBBBB")):
            cur.execute(
                "INSERT INTO user_channel_identities "
                "(tenant_id, user_id, channel, native_id, paired_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tenant, f"u-{tenant}", CHANNEL, native, "cli:test"),
            )
            cur.execute(
                "INSERT INTO channel_pairing_codes "
                "(tenant_id, channel, native_id, code_hash, expires_at) "
                "VALUES (%s, %s, %s, %s, NOW() + interval '10 minutes')",
                (tenant, CHANNEL, native + "X", f"hash-{tenant}"),
            )
    return db


def _tenants_visible(db, table: str, tenant: str | None) -> list[str]:
    with db.cursor() as cur:
        cur.execute("SET ROLE robothor_app")
        try:
            if tenant is not None:
                cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
            cur.execute(f"SELECT tenant_id FROM {table} ORDER BY tenant_id")
            return [row[0] for row in cur.fetchall()]
        finally:
            cur.execute("SELECT set_config('app.tenant_id', '', false)")
            cur.execute("RESET ROLE")


def test_user_channel_identities_is_rls_isolated(channel_db):
    assert _tenants_visible(channel_db, "user_channel_identities", TENANT_A) == [TENANT_A]
    assert _tenants_visible(channel_db, "user_channel_identities", TENANT_B) == [TENANT_B]


def test_channel_pairing_codes_is_rls_isolated(channel_db):
    assert _tenants_visible(channel_db, "channel_pairing_codes", TENANT_A) == [TENANT_A]
    assert _tenants_visible(channel_db, "channel_pairing_codes", TENANT_B) == [TENANT_B]


def test_unbound_connection_still_sees_rows(channel_db):
    """The permissive-when-unbound clause: a migration or the CLI must not be
    locked out of a table it has to maintain."""
    assert _tenants_visible(channel_db, "user_channel_identities", None) == [TENANT_A, TENANT_B]
    assert _tenants_visible(channel_db, "channel_pairing_codes", None) == [TENANT_A, TENANT_B]


@pytest.mark.parametrize(
    ("table", "columns", "values"),
    [
        (
            "user_channel_identities",
            "(tenant_id, user_id, channel, native_id, paired_by)",
            (TENANT_B, "u-smuggled", CHANNEL, "U0SMUGGLED", "cli:test"),
        ),
        (
            "channel_pairing_codes",
            "(tenant_id, channel, native_id, code_hash, expires_at)",
            (TENANT_B, CHANNEL, "U0SMUGGLED", "hash-smuggled", None),
        ),
    ],
)
def test_a_bound_connection_cannot_write_another_tenants_row(channel_db, table, columns, values):
    """WITH CHECK, not just USING. Without it a caller confined on read could
    still INSERT an identity into somebody else's tenant — which is the write
    that actually grants access."""
    placeholders = ", ".join("NOW() + interval '10 minutes'" if v is None else "%s" for v in values)
    params = tuple(v for v in values if v is not None)

    with channel_db.cursor() as cur:
        cur.execute("SET ROLE robothor_app")
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(f"INSERT INTO {table} {columns} VALUES ({placeholders})", params)
    channel_db.rollback()
    with channel_db.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', '', false)")
        cur.execute("RESET ROLE")


def test_a_revoked_binding_does_not_block_a_re_pair(channel_db):
    """The live-row uniqueness is partial. Without the WHERE clause, revoking
    somebody would permanently bar that native id from ever pairing again."""
    with channel_db.cursor() as cur:
        cur.execute(
            "UPDATE user_channel_identities SET revoked_at = NOW() WHERE tenant_id = %s",
            (TENANT_A,),
        )
        cur.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, user_id, channel, native_id, paired_by) VALUES (%s, %s, %s, %s, %s)",
            (TENANT_A, "u-again", CHANNEL, "U0AAAAAAAAA", "cli:test"),
        )
        cur.execute(
            "SELECT count(*) FROM user_channel_identities WHERE native_id = %s", ("U0AAAAAAAAA",)
        )
        assert cur.fetchone()[0] == 2


def test_two_live_bindings_for_one_native_id_are_refused(channel_db):
    with channel_db.cursor() as cur, pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, user_id, channel, native_id, paired_by) VALUES (%s, %s, %s, %s, %s)",
            (TENANT_A, "u-second", CHANNEL, "U0AAAAAAAAA", "cli:test"),
        )
    channel_db.rollback()


def test_two_live_codes_for_one_native_id_are_refused(channel_db):
    with channel_db.cursor() as cur, pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO channel_pairing_codes "
            "(tenant_id, channel, native_id, code_hash, expires_at) "
            "VALUES (%s, %s, %s, %s, NOW() + interval '10 minutes')",
            (TENANT_A, CHANNEL, "U0AAAAAAAAAX", "hash-second"),
        )
    channel_db.rollback()
