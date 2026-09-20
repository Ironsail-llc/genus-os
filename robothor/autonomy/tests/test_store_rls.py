"""Every autonomy migration's RLS was decoration: the product never armed it.

`AutonomyStore.transaction()` opened a bare `psycopg2.connect` and never set
`app.tenant_id`, and every autonomy policy is permissive when that GUC is
unset — `COALESCE(current_setting('app.tenant_id',true),'') IN ('',tenant_id)`.
A non-superuser probe therefore read both tenants' rows through the product's
own connection. `test_enrollment_rls.py` passed only because it sets the GUC
itself, which certifies a control the product never turned on.

These tests go through the real `AutonomyStore.transaction`, as a real
NOSUPERUSER NOBYPASSRLS role, against a real database.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

from robothor.autonomy.models import Delegation, Scope
from robothor.autonomy.store import AutonomyStore

GRANTED = (
    "autonomy_grants",
    "autonomy_operations",
    "autonomy_events",
    "autonomy_terms_snapshots",
    "autonomy_payment_events",
    "vault_resources",
)


def policy(**changes):
    return Delegation(
        agent_ids={"main"},
        origins=frozenset({"https://club.example"}),
        actions={"application"},
        expires_at=datetime.now(UTC) + timedelta(days=1),
        **changes,
    )


@pytest.fixture
def scoped_role(store, identity):
    """An `AutonomyStore` that connects as a role RLS actually applies to."""
    neighbour = Scope(tenant_id="neighbour-" + uuid4().hex, owner_id="bob")
    store.create_grant(identity, policy())
    store.create_grant(neighbour, policy())

    role = "autonomy_rls_" + uuid4().hex
    admin = store._connect()
    with admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        for table in GRANTED:
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
    admin.close()

    def connect():
        conn = store._connect()
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        return conn

    scoped = AutonomyStore(connect, keys={"v1": b"x" * 32}, key_id="v1")
    try:
        yield scoped, neighbour
    finally:
        cleanup = store._connect()
        with cleanup, cleanup.cursor() as cur:
            for table in GRANTED:
                cur.execute(
                    sql.SQL("REVOKE ALL ON {} FROM {}").format(
                        sql.Identifier(table), sql.Identifier(role)
                    )
                )
            cur.execute(
                sql.SQL("REVOKE USAGE ON SCHEMA public FROM {}").format(sql.Identifier(role))
            )
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        cleanup.close()


def test_every_call_site_with_a_scope_binds_it():
    """A new unbound `transaction()` is how this control goes back to sleep.

    The check is deliberately dumb: if the enclosing function was handed a
    `scope`, its transaction has to carry it. The four that are genuinely
    cross-tenant have no `scope` parameter at all, so they are excluded by
    construction rather than by a name list that would drift.
    """
    import ast
    import pathlib

    unbound = []
    for path in sorted(pathlib.Path("robothor/autonomy").rglob("*.py")):
        if "tests" in path.parts:
            continue
        source = path.read_text()
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if "scope" not in {a.arg for a in node.args.args + node.args.kwonlyargs}:
                continue
            unbound.extend(
                f"{path}:{number} in {node.name}()"
                for number in range(node.lineno, (node.end_lineno or node.lineno) + 1)
                if ".transaction()" in lines[number - 1]
            )
    assert unbound == [], "these hold a scope and do not bind it: " + ", ".join(unbound)


def test_a_scoped_transaction_binds_the_tenant_guc(scoped_role, identity):
    scoped, _ = scoped_role
    with scoped.transaction(identity) as cur:
        cur.execute("SELECT current_setting('app.tenant_id', true) AS tenant")
        assert cur.fetchone()["tenant"] == identity.tenant_id


def test_an_unfiltered_read_cannot_reach_the_neighbouring_tenant(scoped_role, identity):
    """The query has NO tenant predicate on purpose. Before this, the probe got
    both tenants back — which is the whole point of the policies existing."""
    scoped, neighbour = scoped_role
    with scoped.transaction(identity) as cur:
        cur.execute("SELECT DISTINCT tenant_id FROM autonomy_grants")
        assert [row["tenant_id"] for row in cur.fetchall()] == [identity.tenant_id]

    with scoped.transaction(neighbour) as cur:
        cur.execute("SELECT DISTINCT tenant_id FROM autonomy_grants")
        assert [row["tenant_id"] for row in cur.fetchall()] == [neighbour.tenant_id]


def test_a_write_into_a_foreign_tenant_is_refused_by_the_database(scoped_role, identity):
    scoped, neighbour = scoped_role
    with pytest.raises(psycopg2.errors.InsufficientPrivilege), scoped.transaction(identity) as cur:
        cur.execute(
            "INSERT INTO autonomy_grants(id,tenant_id,owner_id,policy) VALUES (%s,%s,%s,%s)",
            (str(uuid4()), neighbour.tenant_id, "bob", "{}"),
        )


def test_without_a_scope_the_policies_are_still_permissive(scoped_role, identity):
    """This is the defect, kept as a characterisation rather than a claim.

    The autonomy policies match `'' IN ('', tenant_id)`, so an unbound
    connection sees everything. That branch is deliberate platform-wide — the
    migrator, `psql` and the CLI all run unbound — which is exactly why the
    binding above has to be the product's job. Making the policies themselves
    non-permissive is NOT a policy flip: `rotate_resource_keyring` re-seals
    every owner's resources and `purge_expired` sweeps every tenant, both by
    design, so it needs a privileged-role concept this branch does not have.
    """
    scoped, neighbour = scoped_role
    with scoped.transaction() as cur:
        cur.execute("SELECT DISTINCT tenant_id FROM autonomy_grants")
        seen = {row["tenant_id"] for row in cur.fetchall()}
    assert {identity.tenant_id, neighbour.tenant_id} <= seen


def test_the_binding_does_not_outlive_its_transaction(scoped_role, identity):
    """`set_config(..., is_local => true)`. A binding that survived the commit
    would be a worse bug than none: the next caller would inherit it."""
    scoped, _ = scoped_role
    with scoped.transaction(identity) as cur:
        cur.execute("SELECT 1")
    with scoped.transaction() as cur:
        cur.execute("SELECT current_setting('app.tenant_id', true) AS tenant")
        assert cur.fetchone()["tenant"] in (None, "")
