"""Migration 169: company hierarchy, company identifiers and affiliations.

The CRM held people and companies as flat lists. This migration adds
``crm_companies.parent_company_id``, ``crm_company_identifiers`` (a company's
ids in outside systems) and ``crm_affiliations`` (a person's role at a
company). These run it against a real scratch database at the point in the
chain just before it, with rows already in the tables it touches.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATION = "169_crm_affiliations"
PREVIOUS = "167_agent_schedules_tenant_key"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
NEW_TABLES = ("crm_company_identifiers", "crm_affiliations")


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def _seed_tenants(cur: Any) -> None:
    cur.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES "
        "('tenant-a', 'Tenant A'), ('tenant-b', 'Tenant B') ON CONFLICT DO NOTHING"
    )


def _company(cur: Any, tenant: str, name: str, parent: str | None = None) -> str:
    cid = str(uuid.uuid4())
    if parent is None:
        cur.execute(
            "INSERT INTO crm_companies (id, name, tenant_id) VALUES (%s, %s, %s)",
            (cid, name, tenant),
        )
    else:
        cur.execute(
            "INSERT INTO crm_companies (id, name, tenant_id, parent_company_id) "
            "VALUES (%s, %s, %s, %s)",
            (cid, name, tenant, parent),
        )
    return cid


def _person(cur: Any, tenant: str, first: str) -> str:
    pid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO crm_people (id, first_name, last_name, tenant_id) VALUES (%s, %s, 'Test', %s)",
        (pid, first, tenant),
    )
    return pid


def _snapshot(cur: Any) -> tuple[list[Any], list[Any]]:
    cur.execute("SELECT id, name, tenant_id, updated_at FROM crm_companies ORDER BY id")
    companies = cur.fetchall()
    cur.execute("SELECT id, first_name, company_id, tenant_id, updated_at FROM crm_people ORDER BY id")
    return companies, cur.fetchall()


@pytest.mark.integration
def test_existing_rows_are_unchanged_on_a_populated_database(scratch_db) -> None:
    db, _ = scratch_db(through=PREVIOUS)
    cur = db.cursor()
    _seed_tenants(cur)
    acme = _company(cur, "tenant-a", "Acme")
    _company(cur, "tenant-b", "Globex")
    pid = _person(cur, "tenant-a", "Alice")
    cur.execute("UPDATE crm_people SET company_id = %s WHERE id = %s", (acme, pid))
    before = _snapshot(cur)

    scratch_db(through=MIGRATION)

    assert _snapshot(cur) == before
    cur.execute("SELECT count(*) FROM crm_companies WHERE parent_company_id IS NOT NULL")
    assert cur.fetchone() == (0,)


@pytest.mark.integration
def test_a_rerun_changes_nothing(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    parent = _company(cur, "tenant-a", "Parent")
    _company(cur, "tenant-a", "Child", parent=parent)

    cur.execute(SQL_FILE.read_text())

    cur.execute("SELECT count(*) FROM crm_companies WHERE parent_company_id = %s", (parent,))
    assert cur.fetchone() == (1,)


@pytest.mark.integration
@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_tables_carry_forced_row_level_security(scratch_db, table: str) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass",
        (table,),
    )
    assert cur.fetchone() == (True, True)
    cur.execute("SELECT polname FROM pg_policy WHERE polrelid = %s::regclass", (table,))
    assert cur.fetchall() == [("tenant_isolation",)]


@pytest.mark.integration
def test_a_company_cannot_be_its_own_parent(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    cid = _company(cur, "tenant-a", "Acme")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE crm_companies SET parent_company_id = id WHERE id = %s", (cid,))


@pytest.mark.integration
def test_a_company_cannot_become_its_own_ancestor(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    top = _company(cur, "tenant-a", "Top")
    mid = _company(cur, "tenant-a", "Mid", parent=top)
    leaf = _company(cur, "tenant-a", "Leaf", parent=mid)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE crm_companies SET parent_company_id = %s WHERE id = %s", (leaf, top))


@pytest.mark.integration
def test_a_parent_must_be_in_the_same_tenant(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    foreign = _company(cur, "tenant-b", "Elsewhere")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _company(cur, "tenant-a", "Here", parent=foreign)


@pytest.mark.integration
def test_affiliation_and_identifier_must_match_the_tenant_of_what_they_link(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    company_b = _company(cur, "tenant-b", "Elsewhere")
    person_a = _person(cur, "tenant-a", "Alice")
    company_a = _company(cur, "tenant-a", "Here")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO crm_affiliations (tenant_id, person_id, company_id, role) "
            "VALUES ('tenant-a', %s, %s, 'owner')",
            (person_a, company_b),
        )
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO crm_company_identifiers (tenant_id, system, identifier, company_id) "
            "VALUES ('tenant-a', 'test-system', 'x-1', %s)",
            (company_b,),
        )
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO crm_affiliations (tenant_id, person_id, company_id, role) "
            "VALUES ('tenant-b', %s, %s, 'owner')",
            (person_a, company_a),
        )


@pytest.mark.integration
def test_the_unique_keys_are_per_tenant(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    a = _company(cur, "tenant-a", "Acme")
    b = _company(cur, "tenant-b", "Acme")
    sql = (
        "INSERT INTO crm_company_identifiers (tenant_id, system, identifier, company_id) "
        "VALUES (%s, 'test-system', 'acme-1', %s)"
    )
    cur.execute(sql, ("tenant-a", a))
    cur.execute(sql, ("tenant-b", b))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(sql, ("tenant-a", a))

    p = _person(cur, "tenant-a", "Alice")
    aff = (
        "INSERT INTO crm_affiliations (tenant_id, person_id, company_id, role) "
        "VALUES ('tenant-a', %s, %s, 'owner')"
    )
    cur.execute(aff, (p, a))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(aff, (p, a))


def _as_app(cur: Any, tenant: str) -> None:
    cur.execute("SET ROLE robothor_app")
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))


@pytest.mark.integration
def test_tenant_b_cannot_read_or_write_tenant_a_rows(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    parent = _company(cur, "tenant-a", "Parent A")
    child = _company(cur, "tenant-a", "Child A", parent=parent)
    person = _person(cur, "tenant-a", "Alice")
    cur.execute(
        "INSERT INTO crm_company_identifiers (tenant_id, system, identifier, company_id) "
        "VALUES ('tenant-a', 'test-system', 'a-1', %s)",
        (parent,),
    )
    cur.execute(
        "INSERT INTO crm_affiliations (tenant_id, person_id, company_id, role) "
        "VALUES ('tenant-a', %s, %s, 'owner')",
        (person, child),
    )

    _as_app(cur, "tenant-b")
    for table in (*NEW_TABLES, "crm_companies"):
        cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - constant names
        assert cur.fetchone() == (0,), table
    cur.execute("UPDATE crm_affiliations SET role = 'admin'")
    assert cur.rowcount == 0
    cur.execute("UPDATE crm_company_identifiers SET identifier = 'stolen'")
    assert cur.rowcount == 0
    # Refused either by the policy's WITH CHECK or, first, by the tenant guard
    # that cannot see tenant A's person from a tenant B connection.
    with pytest.raises((psycopg2.errors.InsufficientPrivilege, psycopg2.errors.CheckViolation)):
        cur.execute(
            "INSERT INTO crm_affiliations (tenant_id, person_id, company_id, role) "
            "VALUES ('tenant-a', %s, %s, 'admin')",
            (person, child),
        )

    cur.execute("RESET ROLE")
    _as_app(cur, "tenant-a")
    cur.execute("SELECT count(*) FROM crm_affiliations")
    assert cur.fetchone() == (1,)
    cur.execute("SELECT count(*) FROM crm_company_identifiers")
    assert cur.fetchone() == (1,)
    cur.execute("RESET ROLE")


@pytest.mark.integration
def test_closing_keeps_the_row_and_deleting_a_person_or_company_cascades(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    company = _company(cur, "tenant-a", "Acme")
    person = _person(cur, "tenant-a", "Alice")
    cur.execute(
        "INSERT INTO crm_affiliations (tenant_id, person_id, company_id, role) "
        "VALUES ('tenant-a', %s, %s, 'owner') RETURNING is_active, since IS NOT NULL, until",
        (person, company),
    )
    assert cur.fetchone() == (True, True, None)
    cur.execute(
        "INSERT INTO crm_company_identifiers (tenant_id, system, identifier, company_id) "
        "VALUES ('tenant-a', 'test-system', 'acme-1', %s)",
        (company,),
    )
    cur.execute("DELETE FROM crm_people WHERE id = %s", (person,))
    cur.execute("SELECT count(*) FROM crm_affiliations")
    assert cur.fetchone() == (0,)
    cur.execute("DELETE FROM crm_companies WHERE id = %s", (company,))
    cur.execute("SELECT count(*) FROM crm_company_identifiers")
    assert cur.fetchone() == (0,)


@pytest.mark.integration
def test_deleting_a_parent_leaves_children_without_one(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    cur = db.cursor()
    _seed_tenants(cur)
    parent = _company(cur, "tenant-a", "Parent")
    child = _company(cur, "tenant-a", "Child", parent=parent)
    cur.execute("DELETE FROM crm_companies WHERE id = %s", (parent,))
    cur.execute("SELECT parent_company_id FROM crm_companies WHERE id = %s", (child,))
    assert cur.fetchone() == (None,)
