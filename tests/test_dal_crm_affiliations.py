"""Company hierarchy, external identifiers and affiliations in the CRM DAL.

A plugin that syncs an organization chart from an outside system keys every
row by that system's id, never by name: a renamed company stays one row, two
companies that share a name stay two. These run against the real test
database; each test rolls back.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import psycopg2
import pytest

from robothor.constants import DEFAULT_TENANT

pytestmark = pytest.mark.integration

SYSTEM = "test-system"
OTHER_TENANT = "test-tenant-b"


@pytest.fixture
def other_tenant(db_cursor):
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Tenant B') ON CONFLICT DO NOTHING",
        (OTHER_TENANT,),
    )
    return OTHER_TENANT


def _ext(prefix: str = "x") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _count(cur: Any, sql: str, params: tuple[Any, ...]) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return int(next(iter(row.values())) if isinstance(row, dict) else row[0])


# ── upsert_company_by_identifier ─────────────────────────────────────────────


class TestUpsertCompanyByIdentifier:
    def test_creates_then_is_a_no_op(self, db_cursor, mock_get_connection):
        from robothor.crm.dal import get_company, upsert_company_by_identifier

        ext = _ext()
        first = upsert_company_by_identifier(SYSTEM, ext, name="Acme Holdings")
        assert first["created"] is True
        assert get_company(first["id"])["name"] == "Acme Holdings"
        db_cursor.execute("SELECT updated_at FROM crm_companies WHERE id = %s", (first["id"],))
        stamp = db_cursor.fetchone()["updated_at"]

        again = upsert_company_by_identifier(SYSTEM, ext, name="Acme Holdings")
        assert again == {**first, "created": False, "updated": False}
        db_cursor.execute("SELECT updated_at FROM crm_companies WHERE id = %s", (first["id"],))
        assert db_cursor.fetchone()["updated_at"] == stamp
        assert (
            _count(
                db_cursor, "SELECT count(*) FROM crm_companies WHERE name = %s", ("Acme Holdings",)
            )
            == 1
        )

    def test_a_changed_name_renames_the_same_row(self, db_cursor, mock_get_connection):
        from robothor.crm.dal import get_company, upsert_company_by_identifier

        ext = _ext()
        first = upsert_company_by_identifier(SYSTEM, ext, name="Old Name")
        renamed = upsert_company_by_identifier(SYSTEM, ext, name="New Name")
        assert renamed["id"] == first["id"]
        assert renamed["created"] is False
        assert renamed["updated"] is True
        assert get_company(first["id"])["name"] == "New Name"

    def test_never_matches_by_name(self, db_cursor, mock_get_connection):
        from robothor.crm.dal import create_company, upsert_company_by_identifier

        existing = create_company("Same Name Co")
        a = upsert_company_by_identifier(SYSTEM, _ext(), name="Same Name Co")
        b = upsert_company_by_identifier(SYSTEM, _ext(), name="Same Name Co")
        assert len({existing, a["id"], b["id"]}) == 3

    def test_sets_parent_and_fields(self, db_cursor, mock_get_connection):
        from robothor.crm.dal import get_company, upsert_company_by_identifier

        parent = upsert_company_by_identifier(SYSTEM, _ext(), name="Parent Co")
        child = upsert_company_by_identifier(
            SYSTEM,
            _ext(),
            name="Child Co",
            parent_company_id=parent["id"],
            fields={"address_city": "Springfield", "domain_name": "example.com"},
        )
        got = get_company(child["id"])
        assert got["parentCompanyId"] == parent["id"]
        assert got["domainName"] == "example.com"

    def test_rejects_unknown_fields_and_blank_keys(self, mock_get_connection):
        from robothor.crm.dal import upsert_company_by_identifier

        with pytest.raises(ValueError):
            upsert_company_by_identifier(SYSTEM, _ext(), name="Acme", fields={"tenant_id": "x"})
        with pytest.raises(ValueError):
            upsert_company_by_identifier(SYSTEM, "  ", name="Acme")
        with pytest.raises(ValueError):
            upsert_company_by_identifier("", _ext(), name="Acme")
        with pytest.raises(ValueError):
            upsert_company_by_identifier(SYSTEM, _ext(), name=" ")

    def test_a_soft_deleted_company_is_reported_not_resurrected(
        self, db_cursor, mock_get_connection
    ):
        from robothor.crm.dal import delete_company, get_company, upsert_company_by_identifier

        ext = _ext()
        first = upsert_company_by_identifier(SYSTEM, ext, name="Gone Co")
        assert delete_company(first["id"])

        again = upsert_company_by_identifier(SYSTEM, ext, name="Gone Co Renamed")
        assert again["id"] == first["id"]
        assert again["deleted"] is True
        assert again["created"] is False
        assert get_company(first["id"]) is None
        db_cursor.execute("SELECT name FROM crm_companies WHERE id = %s", (first["id"],))
        assert db_cursor.fetchone()["name"] == "Gone Co"

        restored = upsert_company_by_identifier(SYSTEM, ext, name="Back Co", restore=True)
        assert restored["deleted"] is False
        assert get_company(first["id"])["name"] == "Back Co"

    def test_identifiers_are_per_tenant(self, other_tenant, mock_get_connection):
        from robothor.crm.dal import get_company_by_identifier, upsert_company_by_identifier

        ext = _ext()
        a = upsert_company_by_identifier(SYSTEM, ext, name="Acme")
        b = upsert_company_by_identifier(SYSTEM, ext, name="Acme", tenant_id=other_tenant)
        assert a["id"] != b["id"]
        assert get_company_by_identifier(SYSTEM, ext)["id"] == a["id"]
        assert get_company_by_identifier(SYSTEM, ext, tenant_id=other_tenant)["id"] == b["id"]

    def test_a_parent_in_another_tenant_is_refused(self, other_tenant, mock_get_connection):
        from robothor.crm.dal import upsert_company_by_identifier

        foreign = upsert_company_by_identifier(SYSTEM, _ext(), name="B", tenant_id=other_tenant)
        with pytest.raises(ValueError):
            upsert_company_by_identifier(SYSTEM, _ext(), name="A", parent_company_id=foreign["id"])


class TestHierarchy:
    def test_children_and_parent_chain(self, mock_get_connection):
        from robothor.crm.dal import (
            delete_company,
            get_company_parent_chain,
            list_company_children,
            upsert_company_by_identifier,
        )

        top = upsert_company_by_identifier(SYSTEM, _ext(), name="Top")
        mid = upsert_company_by_identifier(SYSTEM, _ext(), name="Mid", parent_company_id=top["id"])
        leaf_a = upsert_company_by_identifier(
            SYSTEM, _ext(), name="Leaf A", parent_company_id=mid["id"]
        )
        leaf_b = upsert_company_by_identifier(
            SYSTEM, _ext(), name="Leaf B", parent_company_id=mid["id"]
        )
        assert [c["id"] for c in list_company_children(mid["id"])] == [leaf_a["id"], leaf_b["id"]]
        assert [c["id"] for c in get_company_parent_chain(leaf_a["id"])] == [mid["id"], top["id"]]
        assert get_company_parent_chain(top["id"]) == []

        delete_company(leaf_b["id"])
        assert [c["id"] for c in list_company_children(mid["id"])] == [leaf_a["id"]]

    def test_set_company_parent_refuses_a_cycle_and_can_clear(self, mock_get_connection):
        from robothor.crm.dal import get_company, set_company_parent, upsert_company_by_identifier

        top = upsert_company_by_identifier(SYSTEM, _ext(), name="Top")
        leaf = upsert_company_by_identifier(
            SYSTEM, _ext(), name="Leaf", parent_company_id=top["id"]
        )
        with pytest.raises(ValueError):
            set_company_parent(top["id"], leaf["id"])
        assert set_company_parent(leaf["id"], None) is True
        assert get_company(leaf["id"])["parentCompanyId"] is None


# ── upsert_person_by_identifier ──────────────────────────────────────────────


class TestUpsertPersonByIdentifier:
    def test_creates_maps_and_is_idempotent(self, db_cursor, mock_get_connection):
        from robothor.crm.dal import (
            get_person,
            get_person_by_identifier,
            upsert_person_by_identifier,
        )

        ext = _ext("user")
        first = upsert_person_by_identifier(
            SYSTEM, ext, first_name="Alice", last_name="Example", email="Alice@Example.com"
        )
        assert first["created"] is True
        person = get_person(first["id"])
        assert person["name"]["firstName"] == "Alice"
        assert get_person_by_identifier(SYSTEM, ext)["id"] == first["id"]
        db_cursor.execute(
            "SELECT person_id FROM contact_identifiers WHERE tenant_id = %s AND channel = %s "
            "AND identifier = %s",
            (DEFAULT_TENANT, SYSTEM, ext),
        )
        assert str(db_cursor.fetchone()["person_id"]) == first["id"]

        again = upsert_person_by_identifier(
            SYSTEM, ext, first_name="Alice", last_name="Example", email="alice@example.com"
        )
        assert again == {**first, "created": False, "updated": False}

    def test_a_changed_name_updates_the_same_person(self, mock_get_connection):
        from robothor.crm.dal import get_person, upsert_person_by_identifier

        ext = _ext("user")
        first = upsert_person_by_identifier(SYSTEM, ext, first_name="Alice", last_name="Old")
        second = upsert_person_by_identifier(SYSTEM, ext, first_name="Alice", last_name="New")
        assert second["id"] == first["id"]
        assert second["updated"] is True
        assert get_person(first["id"])["name"]["lastName"] == "New"

    def test_never_matches_by_name_or_email(self, mock_get_connection):
        from robothor.crm.dal import create_person, upsert_person_by_identifier

        existing = create_person("Bob", "Example", email="bob@example.com")
        made = upsert_person_by_identifier(
            SYSTEM, _ext("user"), first_name="Bob", last_name="Example", email="bob@example.com"
        )
        assert made["id"] != existing

    def test_claims_an_identifier_row_with_no_person(self, db_cursor, mock_get_connection):
        from robothor.crm.dal import upsert_person_by_identifier

        ext = _ext("user")
        db_cursor.execute(
            "INSERT INTO contact_identifiers (tenant_id, channel, identifier) VALUES (%s, %s, %s)",
            (DEFAULT_TENANT, SYSTEM, ext),
        )
        made = upsert_person_by_identifier(SYSTEM, ext, first_name="Carol")
        assert made["created"] is True
        db_cursor.execute(
            "SELECT person_id FROM contact_identifiers WHERE tenant_id = %s AND channel = %s "
            "AND identifier = %s",
            (DEFAULT_TENANT, SYSTEM, ext),
        )
        assert str(db_cursor.fetchone()["person_id"]) == made["id"]

    def test_a_soft_deleted_person_is_reported_not_resurrected(self, mock_get_connection):
        from robothor.crm.dal import delete_person, get_person, upsert_person_by_identifier

        ext = _ext("user")
        first = upsert_person_by_identifier(SYSTEM, ext, first_name="Dana")
        delete_person(first["id"])
        again = upsert_person_by_identifier(SYSTEM, ext, first_name="Dana")
        assert again["deleted"] is True and again["id"] == first["id"]
        assert get_person(first["id"]) is None
        restored = upsert_person_by_identifier(SYSTEM, ext, first_name="Dana", restore=True)
        assert restored["deleted"] is False
        assert get_person(first["id"]) is not None

    def test_rejects_an_invalid_name(self, mock_get_connection):
        from robothor.crm.dal import upsert_person_by_identifier

        with pytest.raises(ValueError):
            upsert_person_by_identifier(SYSTEM, _ext("user"), first_name="x")


# ── affiliations ─────────────────────────────────────────────────────────────


@pytest.fixture
def org(mock_get_connection):
    from robothor.crm.dal import upsert_company_by_identifier, upsert_person_by_identifier

    parent = upsert_company_by_identifier(SYSTEM, _ext(), name="Parent Group")["id"]
    company = upsert_company_by_identifier(
        SYSTEM, _ext(), name="Child Office", parent_company_id=parent
    )["id"]
    alice = upsert_person_by_identifier(SYSTEM, _ext("user"), first_name="Alice")["id"]
    bob = upsert_person_by_identifier(SYSTEM, _ext("user"), first_name="Bob")["id"]
    return {"parent": parent, "company": company, "alice": alice, "bob": bob}


class TestAffiliations:
    def test_set_is_idempotent_and_updates_attributes(self, db_cursor, org):
        from robothor.crm.dal import list_affiliations, set_affiliation

        first = set_affiliation(org["alice"], org["company"], "Owner", attributes={"a": 1})
        assert first["created"] is True and first["changed"] is True
        assert first["role"] == "owner"
        assert first["isActive"] is True and first["until"] is None

        again = set_affiliation(org["alice"], org["company"], "owner", attributes={"a": 1})
        assert again["id"] == first["id"]
        assert again["created"] is False and again["changed"] is False
        assert again["updatedAt"] == first["updatedAt"]

        changed = set_affiliation(org["alice"], org["company"], "owner", attributes={"a": 2})
        assert changed["changed"] is True and changed["attributes"] == {"a": 2}

        kept = set_affiliation(org["alice"], org["company"], "owner")
        assert kept["changed"] is False and kept["attributes"] == {"a": 2}
        assert len(list_affiliations(person_id=org["alice"])) == 1

    def test_close_keeps_the_row(self, db_cursor, org):
        from robothor.crm.dal import close_affiliation, list_affiliations, set_affiliation

        made = set_affiliation(org["alice"], org["company"], "manager")
        closed = close_affiliation(org["alice"], org["company"], "manager")
        assert closed["id"] == made["id"]
        assert closed["isActive"] is False and closed["until"] is not None
        assert list_affiliations(person_id=org["alice"]) == []
        assert [a["id"] for a in list_affiliations(person_id=org["alice"], active_only=False)] == [
            made["id"]
        ]
        assert (
            close_affiliation(org["alice"], org["company"], "manager")["until"] == closed["until"]
        )
        assert close_affiliation(org["bob"], org["company"], "manager") is None

        reopened = set_affiliation(org["alice"], org["company"], "manager")
        assert reopened["isActive"] is True and reopened["until"] is None

    def test_list_filters_and_names(self, org):
        from robothor.crm.dal import list_affiliations, set_affiliation

        set_affiliation(org["alice"], org["company"], "owner")
        set_affiliation(org["bob"], org["company"], "provider")
        set_affiliation(org["bob"], org["parent"], "admin")

        at_company = list_affiliations(company_id=org["company"])
        assert {(a["personId"], a["role"]) for a in at_company} == {
            (org["alice"], "owner"),
            (org["bob"], "provider"),
        }
        assert at_company[0]["companyName"] == "Child Office"
        assert all(a["personName"] for a in at_company)
        assert [
            a["role"] for a in list_affiliations(company_id=org["company"], role="provider")
        ] == ["provider"]
        assert {a["companyId"] for a in list_affiliations(person_id=org["bob"])} == {
            org["company"],
            org["parent"],
        }

    def test_refuses_unknown_or_foreign_rows(self, other_tenant, org):
        from robothor.crm.dal import (
            list_affiliations,
            set_affiliation,
            upsert_company_by_identifier,
        )

        foreign = upsert_company_by_identifier(SYSTEM, _ext(), name="B Co", tenant_id=other_tenant)
        with pytest.raises(ValueError):
            set_affiliation(org["alice"], foreign["id"], "owner")
        with pytest.raises(ValueError):
            set_affiliation(org["alice"], foreign["id"], "owner", tenant_id=other_tenant)
        with pytest.raises(ValueError):
            set_affiliation(org["alice"], org["company"], "  ")
        set_affiliation(org["alice"], org["company"], "owner")
        assert list_affiliations(person_id=org["alice"], tenant_id=other_tenant) == []

    def test_contact_360_carries_affiliations_with_the_parent_chain(self, org):
        from robothor.crm.dal import get_contact_360, set_affiliation

        set_affiliation(org["alice"], org["company"], "owner")
        view = get_contact_360(org["alice"])
        assert "person" in view and "timeline" in view  # existing shape kept
        (aff,) = view["affiliations"]
        assert aff["companyId"] == org["company"]
        assert [c["id"] for c in aff["parentChain"]] == [org["parent"]]

    def test_company_view(self, org):
        from robothor.crm.dal import get_company_view, set_affiliation

        set_affiliation(org["alice"], org["parent"], "owner")
        set_affiliation(org["bob"], org["company"], "provider")
        view = get_company_view(org["parent"])
        assert view["company"]["id"] == org["parent"]
        assert view["parent"] is None
        assert [c["id"] for c in view["children"]] == [org["company"]]
        assert [(a["personId"], a["role"]) for a in view["affiliations"]] == [
            (org["alice"], "owner")
        ]
        assert len(view["identifiers"]) == 1
        child_view = get_company_view(org["company"])
        assert child_view["parent"]["id"] == org["parent"]
        assert get_company_view(str(uuid.uuid4())) is None


# ── merges carry the new rows ────────────────────────────────────────────────


class TestMerges:
    def test_merge_people_moves_and_dedupes_affiliations(self, db_cursor, org):
        from robothor.crm.dal import list_affiliations, merge_people, set_affiliation

        set_affiliation(org["alice"], org["company"], "owner", attributes={"keep": "alice"})
        set_affiliation(org["bob"], org["company"], "owner", attributes={"from": "bob"})
        set_affiliation(org["bob"], org["parent"], "admin")
        set_affiliation(org["alice"], org["parent"], "manager", is_active=False)
        set_affiliation(org["bob"], org["parent"], "manager")

        assert merge_people(org["alice"], org["bob"]) is not None

        rows = {
            (a["companyId"], a["role"]): a
            for a in list_affiliations(person_id=org["alice"], active_only=False)
        }
        assert set(rows) == {
            (org["company"], "owner"),
            (org["parent"], "admin"),
            (org["parent"], "manager"),
        }
        assert rows[(org["company"], "owner")]["attributes"] == {"keep": "alice", "from": "bob"}
        assert rows[(org["parent"], "manager")]["isActive"] is True
        assert (
            _count(
                db_cursor,
                "SELECT count(*) FROM crm_affiliations WHERE person_id = %s",
                (org["bob"],),
            )
            == 0
        )

    def test_merge_companies_moves_identifiers_affiliations_and_children(self, db_cursor, org):
        from robothor.crm.dal import (
            get_company,
            get_company_by_identifier,
            list_affiliations,
            list_company_children,
            merge_companies,
            set_affiliation,
            upsert_company_by_identifier,
        )

        loser_ext = _ext()
        loser = upsert_company_by_identifier(SYSTEM, loser_ext, name="Duplicate Office")["id"]
        grandchild = upsert_company_by_identifier(
            SYSTEM, _ext(), name="Satellite", parent_company_id=loser
        )["id"]
        set_affiliation(org["alice"], org["company"], "owner")
        set_affiliation(org["alice"], loser, "owner", attributes={"x": 1})
        set_affiliation(org["bob"], loser, "provider")

        assert merge_companies(org["company"], loser) is not None

        assert get_company_by_identifier(SYSTEM, loser_ext)["id"] == org["company"]
        assert {
            (a["personId"], a["role"]) for a in list_affiliations(company_id=org["company"])
        } == {
            (org["alice"], "owner"),
            (org["bob"], "provider"),
        }
        assert [c["id"] for c in list_company_children(org["company"])] == [grandchild]
        assert get_company(org["company"])["parentCompanyId"] == org["parent"]

    def test_merge_companies_when_keeper_sits_under_loser(self, org):
        from robothor.crm.dal import (
            get_company,
            list_company_children,
            merge_companies,
            upsert_company_by_identifier,
        )

        # parent -> company(keeper candidate) ; merge parent INTO company.
        sibling = upsert_company_by_identifier(
            SYSTEM, _ext(), name="Sibling", parent_company_id=org["parent"]
        )["id"]
        assert merge_companies(org["company"], org["parent"]) is not None
        assert get_company(org["company"])["parentCompanyId"] is None
        assert [c["id"] for c in list_company_children(org["company"])] == [sibling]

    def test_merge_companies_fills_an_empty_parent_without_a_cycle(self, org):
        from robothor.crm.dal import get_company, merge_companies, upsert_company_by_identifier

        # keeper = parent (no parent of its own); loser hangs under keeper's child.
        loser = upsert_company_by_identifier(
            SYSTEM, _ext(), name="Under Child", parent_company_id=org["company"]
        )["id"]
        assert merge_companies(org["parent"], loser) is not None
        assert get_company(org["parent"])["parentCompanyId"] is None


# ── concurrency (real connections, committed rows) ───────────────────────────


@pytest.fixture
def committed_tenant(db_dsn):
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(db_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Race')", (tenant,))
    yield tenant
    with conn.cursor() as cur:
        cur.execute("DELETE FROM crm_affiliations WHERE tenant_id = %s", (tenant,))
        cur.execute("DELETE FROM crm_company_identifiers WHERE tenant_id = %s", (tenant,))
        cur.execute("DELETE FROM contact_identifiers WHERE tenant_id = %s", (tenant,))
        cur.execute("DELETE FROM crm_people WHERE tenant_id = %s", (tenant,))
        cur.execute("DELETE FROM crm_companies WHERE tenant_id = %s", (tenant,))
        cur.execute("DELETE FROM crm_tenants WHERE id = %s", (tenant,))
    conn.close()


def _race(fn, n: int = 8) -> list[Any]:
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            barrier.wait()
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_concurrent_upserts_make_one_row(committed_tenant, db_dsn):
    from robothor.crm.dal import (
        set_affiliation,
        upsert_company_by_identifier,
        upsert_person_by_identifier,
    )

    companies = _race(
        lambda: upsert_company_by_identifier(
            SYSTEM, "race-1", name="Race Co", tenant_id=committed_tenant
        )
    )
    people = _race(
        lambda: upsert_person_by_identifier(
            SYSTEM, "race-u1", first_name="Alice", tenant_id=committed_tenant
        )
    )
    assert len({c["id"] for c in companies}) == 1
    assert sum(c["created"] for c in companies) == 1
    assert len({p["id"] for p in people}) == 1
    assert sum(p["created"] for p in people) == 1

    affs = _race(
        lambda: set_affiliation(
            people[0]["id"], companies[0]["id"], "owner", tenant_id=committed_tenant
        )
    )
    assert len({a["id"] for a in affs}) == 1

    conn = psycopg2.connect(db_dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_companies WHERE tenant_id = %s", (committed_tenant,))
        assert cur.fetchone() == (1,)
        cur.execute("SELECT count(*) FROM crm_people WHERE tenant_id = %s", (committed_tenant,))
        assert cur.fetchone() == (1,)
    conn.close()
