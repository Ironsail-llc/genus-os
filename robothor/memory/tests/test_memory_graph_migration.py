"""The memory graph foundation migration, against real Postgres.

The migration adds three tables (resolution decisions, mentions, relation
evidence) and new columns on facts, entities and relations. Each test builds a
scratch database from the canonical manifest, so nothing here touches the
shared test database other suites read.

What is proved:

* it applies on a fresh chain and again on top of itself;
* every new table is tenant-keyed, references ``crm_tenants`` with no default
  tenant, and carries a forced ``tenant_isolation`` policy;
* the CHECK constraints accept exactly the vocabulary in
  ``robothor.memory.graph_schema`` and refuse anything else;
* the existing ``memory_relations.fact_id`` links become evidence rows;
* the content hash of existing facts still verifies (no hashed field changed);
* the code that predates the migration — ``upsert_entity``, ``add_relation``,
  ``store_fact``, ``get_entity`` — runs unchanged against the migrated schema;
* ``move_workload`` has a classification for every new table.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from robothor.memory import graph_schema as gs

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
_SUFFIX = "_memory_graph_foundation.sql"

NEW_TABLES = ("memory_resolution_decisions", "memory_mentions", "memory_relation_evidence")
TENANT = "test-tenant"


def _entry() -> str:
    """The manifest entry, found by name so a renumbering touches only the file."""
    found = [
        line.strip() for line in MANIFEST.read_text().splitlines() if line.strip().endswith(_SUFFIX)
    ]
    return found[0] if len(found) == 1 else f"crm/missing{_SUFFIX}"


MIGRATION = Path(_entry()).stem
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"


def _previous_migration() -> str:
    from robothor.db.migrate import _discover

    ids = [m.migration_id for m in _discover()]
    return ids[ids.index(MIGRATION) - 1]


def _seed_tenant(cur: Any, tenant: str = TENANT) -> None:
    cur.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Test') ON CONFLICT DO NOTHING",
        (tenant,),
    )


def _columns(cur: Any, table: str) -> dict[str, tuple[str, str, str | None]]:
    cur.execute(
        "SELECT column_name, udt_name, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}


# ─── applies, and applies again ──────────────────────────────────────────


def test_it_applies_on_a_fresh_chain_and_twice_more(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        cur.execute(SQL_FILE.read_text())
        for table in NEW_TABLES:
            cur.execute("SELECT to_regclass(%s)", (table,))
            assert cur.fetchone()[0] == table


def test_the_new_tables_have_the_agreed_columns(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        decisions = _columns(cur, "memory_resolution_decisions")
        assert set(decisions) == {
            "id", "tenant_id", "surface_form", "entity_type", "resolved_entity_id", "method",
            "confidence", "evidence", "decided_at", "reversed_at", "superseded_by",
        }  # fmt: skip
        assert decisions["resolved_entity_id"][1] == "YES"
        assert decisions["reversed_at"][1] == "YES"
        assert decisions["evidence"][0] == "jsonb"

        mentions = _columns(cur, "memory_mentions")
        assert set(mentions) == {
            "id", "tenant_id", "fact_id", "entity_id", "surface_form", "entity_type",
            "decision_id", "created_at",
        }  # fmt: skip
        assert mentions["fact_id"][1] == "NO"
        assert mentions["entity_id"][1] == "YES"
        assert mentions["decision_id"][1] == "YES"

        evidence = _columns(cur, "memory_relation_evidence")
        assert set(evidence) == {"tenant_id", "relation_id", "fact_id", "created_at"}
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'memory_relation_evidence'::regclass AND contype = 'p'"
        )
        assert cur.fetchone()[0] == "PRIMARY KEY (relation_id, fact_id)"


def test_the_existing_tables_gain_nullable_or_defaulted_columns(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        rel = _columns(cur, "memory_relations")
        for col in (
            "predicate", "valid_from", "valid_to", "recorded_at", "expired_at",
            "retired_reason", "origin",
        ):  # fmt: skip
            assert rel[col][1] == "YES", col
        assert rel["is_active"][:2] == ("bool", "NO")
        assert rel["weight"][:2] == ("float8", "NO")
        assert "now()" in (rel["recorded_at"][2] or "")

        facts = _columns(cur, "memory_facts")
        assert facts["origin"][1] == "YES"
        assert facts["event_time"][:2] == ("timestamptz", "YES")
        assert facts["scope_kind"][:2] == ("text", "NO")
        assert facts["scope_id"][1] == "YES"

        ents = _columns(cur, "memory_entities")
        assert ents["crm_person_id"][:2] == ("uuid", "YES")
        assert ents["crm_company_id"][:2] == ("uuid", "YES")
        assert ents["embedding"][:2] == ("vector", "YES")
        assert ents["scope_kind"][:2] == ("text", "NO")
        assert ents["scope_id"][1] == "YES"


def test_the_agreed_indexes_exist(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(
            "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename IN ('memory_mentions', 'memory_relations', 'memory_entities')"
        )
        defs = [" ".join(d.split()) for _t, d in cur.fetchall()]
    assert any("memory_mentions USING btree (tenant_id, entity_id)" in d for d in defs)
    assert any("memory_mentions USING btree (fact_id)" in d for d in defs)
    assert any(
        "memory_relations USING btree (tenant_id, source_entity_id) WHERE is_active" in d
        for d in defs
    )
    assert any(
        "memory_relations USING btree (tenant_id, target_entity_id) WHERE is_active" in d
        for d in defs
    )
    assert any("memory_entities USING btree (tenant_id, lower((name)::text))" in d for d in defs)


# ─── tenancy ─────────────────────────────────────────────────────────────


def test_every_new_table_is_tenant_keyed_with_forced_row_security(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        for table in NEW_TABLES:
            tenant = _columns(cur, table)["tenant_id"]
            assert tenant[:2] == ("text", "NO"), table
            assert tenant[2] is None, f"{table}.tenant_id must not default to a tenant"
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass",
                (table,),
            )
            assert cur.fetchone() == (True, True), table
            cur.execute(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = %s AND policyname = 'tenant_isolation'",
                (table,),
            )
            qual, check = cur.fetchone()
            assert "app.tenant_id" in qual and "app.tenant_id" in check, table
            cur.execute(
                "SELECT 1 FROM pg_constraint c JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "WHERE c.conrelid = %s::regclass AND c.contype = 'f' "
                "AND c.confrelid = 'crm_tenants'::regclass AND a.attname = 'tenant_id'",
                (table,),
            )
            assert cur.fetchone(), f"{table}.tenant_id must reference crm_tenants"


def test_move_workload_classifies_every_tenant_table_of_the_migrated_schema(scratch_db) -> None:
    from psycopg2.extras import RealDictCursor

    from robothor.tenancy import move_workload as mw

    db, _ = scratch_db(through=MIGRATION)
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        mw._refuse_unclassified(cur)  # raises WorkloadMoveError on any gap
        cur.execute(mw._TENANT_TABLES_SQL)
        tables = {r["tbl"] for r in cur.fetchall()}
    for table in NEW_TABLES:
        assert table in tables
        # Tenant memory stays with the tenant, like the facts and edges it joins.
        assert table in mw.UNTOUCHED_TABLES, table
        assert mw.UNTOUCHED_TABLES[table] == mw.UNTOUCHED_TABLES["memory_facts"]


# ─── CHECK constraints ───────────────────────────────────────────────────


@contextmanager
def _rejected(db: Any) -> Iterator[Any]:
    from psycopg2 import errors

    # The scratch connection autocommits: a refused statement leaves nothing behind.
    with db.cursor() as cur, pytest.raises(errors.CheckViolation):
        yield cur


def _world(db: Any) -> dict[str, int]:
    with db.cursor() as cur:
        _seed_tenant(cur)
        cur.execute(
            "INSERT INTO memory_facts (fact_text, category, tenant_id) "
            "VALUES ('Alice works at Acme', 'personal', %s) RETURNING id",
            (TENANT,),
        )
        fact = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) "
            "VALUES ('Alice', 'person', %s), ('Acme', 'organization', %s) RETURNING id",
            (TENANT, TENANT),
        )
        alice, acme = (r[0] for r in cur.fetchall())
        cur.execute(
            "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
            "tenant_id) VALUES (%s, %s, 'works at', %s) RETURNING id",
            (alice, acme, TENANT),
        )
        rel = cur.fetchone()[0]
    return {"fact": fact, "alice": alice, "acme": acme, "relation": rel}


@pytest.mark.parametrize(
    ("statement", "value"),
    [
        ("UPDATE memory_facts SET origin = %s", "bogus"),
        ("UPDATE memory_facts SET origin = %s", "inferred"),  # edges only
        ("UPDATE memory_facts SET scope_kind = %s, scope_id = 'x'", "team"),
        ("UPDATE memory_entities SET scope_kind = %s, scope_id = 'x'", "team"),
        ("UPDATE memory_relations SET origin = %s", "bogus"),
        ("UPDATE memory_relations SET retired_reason = %s", "forgotten"),
    ],
)
def test_check_constraints_refuse_values_outside_the_vocabulary(
    scratch_db, statement: str, value: str
) -> None:
    db, _ = scratch_db(through=MIGRATION)
    _world(db)
    with _rejected(db) as cur:
        cur.execute(statement, (value,))


def test_a_decision_with_an_unknown_method_is_refused(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    _world(db)
    with _rejected(db) as cur:
        cur.execute(
            "INSERT INTO memory_resolution_decisions (tenant_id, surface_form, method) "
            "VALUES (%s, 'Alice', 'guess')",
            (TENANT,),
        )


@pytest.mark.parametrize("table", ["memory_facts", "memory_entities"])
@pytest.mark.parametrize(
    ("kind", "scope_id"), [("shared", "someone"), ("user", None), ("agent", None)]
)
def test_a_scope_id_goes_with_a_private_scope_and_only_with_one(
    scratch_db, table: str, kind: str, scope_id: str | None
) -> None:
    db, _ = scratch_db(through=MIGRATION)
    _world(db)
    with _rejected(db) as cur:
        cur.execute(
            f"UPDATE {table} SET scope_kind = %s, scope_id = %s",  # noqa: S608
            (kind, scope_id),
        )


def test_every_value_of_the_vocabulary_is_accepted(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    ids = _world(db)
    with db.cursor() as cur:
        for origin in gs.FACT_ORIGINS:
            cur.execute("UPDATE memory_facts SET origin = %s", (origin,))
        for origin in gs.RELATION_ORIGINS:
            cur.execute("UPDATE memory_relations SET origin = %s", (origin,))
        for reason in gs.RETIREMENT_REASONS:
            cur.execute(
                "UPDATE memory_relations SET retired_reason = %s, is_active = FALSE, "
                "expired_at = now()",
                (reason,),
            )
        for kind in gs.SCOPE_KINDS:
            scope_id = None if kind == gs.DEFAULT_SCOPE_KIND else "owner-1"
            for table in ("memory_facts", "memory_entities"):
                cur.execute(
                    f"UPDATE {table} SET scope_kind = %s, scope_id = %s",  # noqa: S608
                    (kind, scope_id),
                )
        for method in gs.RESOLUTION_METHODS:
            cur.execute(
                "INSERT INTO memory_resolution_decisions "
                "(tenant_id, surface_form, entity_type, resolved_entity_id, method, confidence) "
                "VALUES (%s, 'Alice', 'person', %s, %s, 0.9)",
                (TENANT, ids["alice"], method),
            )
        # Left unresolved: no entity.
        cur.execute(
            "INSERT INTO memory_resolution_decisions (tenant_id, surface_form, method) "
            "VALUES (%s, 'Somebody', 'context') RETURNING id",
            (TENANT,),
        )


# ─── foreign keys and their delete behaviour ─────────────────────────────


def test_a_deleted_fact_takes_its_mentions_and_evidence_with_it(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    ids = _world(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_resolution_decisions "
            "(tenant_id, surface_form, resolved_entity_id, method) "
            "VALUES (%s, 'Alice', %s, 'exact') RETURNING id",
            (TENANT, ids["alice"]),
        )
        decision = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form, "
            "entity_type, decision_id) VALUES (%s, %s, %s, 'Alice', 'person', %s)",
            (TENANT, ids["fact"], ids["alice"], decision),
        )
        cur.execute(
            "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
            "VALUES (%s, %s, %s)",
            (TENANT, ids["relation"], ids["fact"]),
        )
        cur.execute("DELETE FROM memory_facts WHERE id = %s", (ids["fact"],))
        cur.execute("SELECT count(*) FROM memory_mentions")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM memory_relation_evidence")
        assert cur.fetchone()[0] == 0


def test_a_deleted_relation_takes_its_evidence_with_it(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    ids = _world(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
            "VALUES (%s, %s, %s)",
            (TENANT, ids["relation"], ids["fact"]),
        )
        cur.execute("DELETE FROM memory_relations WHERE id = %s", (ids["relation"],))
        cur.execute("SELECT count(*) FROM memory_relation_evidence")
        assert cur.fetchone()[0] == 0


def test_a_deleted_entity_leaves_its_mentions_unresolved(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    ids = _world(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form) "
            "VALUES (%s, %s, %s, 'Acme')",
            (TENANT, ids["fact"], ids["acme"]),
        )
        cur.execute("DELETE FROM memory_relations")
        cur.execute("DELETE FROM memory_entities WHERE id = %s", (ids["acme"],))
        cur.execute("SELECT entity_id, surface_form FROM memory_mentions")
        assert cur.fetchone() == (None, "Acme")


def test_the_crm_links_are_cleared_when_the_crm_record_goes(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    ids = _world(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_people (tenant_id, first_name) VALUES (%s, 'Alice') RETURNING id",
            (TENANT,),
        )
        person = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO crm_companies (tenant_id, name) VALUES (%s, 'Acme') RETURNING id",
            (TENANT,),
        )
        company = cur.fetchone()[0]
        cur.execute(
            "UPDATE memory_entities SET crm_person_id = %s WHERE id = %s", (person, ids["alice"])
        )
        cur.execute(
            "UPDATE memory_entities SET crm_company_id = %s WHERE id = %s", (company, ids["acme"])
        )
        cur.execute("DELETE FROM crm_people WHERE id = %s", (person,))
        cur.execute("DELETE FROM crm_companies WHERE id = %s", (company,))
        cur.execute(
            "SELECT count(*) FROM memory_entities "
            "WHERE crm_person_id IS NOT NULL OR crm_company_id IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0


def test_the_relation_fact_id_link_keeps_its_delete_behaviour(scratch_db) -> None:
    """Recorded, not changed: the original ``fact_id`` reference is NO ACTION."""
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(
            "SELECT c.confdeltype FROM pg_constraint c JOIN pg_attribute a "
            "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.conrelid = 'memory_relations'::regclass AND c.contype = 'f' "
            "AND a.attname = 'fact_id'"
        )
        assert [r[0] for r in cur.fetchall()] == ["a"]


# ─── existing data ───────────────────────────────────────────────────────


def test_existing_relation_facts_become_evidence_once(scratch_db) -> None:
    db, _ = scratch_db(through=_previous_migration())
    with db.cursor() as cur:
        _seed_tenant(cur)
        cur.execute(
            "INSERT INTO memory_facts (fact_text, category, tenant_id) "
            "VALUES ('Alice works at Acme', 'personal', %s) RETURNING id",
            (TENANT,),
        )
        fact = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) "
            "VALUES ('Alice', 'person', %s), ('Acme', 'organization', %s), "
            "('Bob', 'person', %s) RETURNING id",
            (TENANT, TENANT, TENANT),
        )
        alice, acme, bob = (r[0] for r in cur.fetchall())
        cur.execute(
            "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
            "fact_id, tenant_id) VALUES (%s, %s, 'works at', %s, %s), "
            "(%s, %s, 'knows', NULL, %s) RETURNING id",
            (alice, acme, fact, TENANT, bob, alice, TENANT),
        )
        with_fact, _without = (r[0] for r in cur.fetchall())

    scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())  # again: no duplicate evidence
        cur.execute("SELECT tenant_id, relation_id, fact_id FROM memory_relation_evidence")
        assert cur.fetchall() == [(TENANT, with_fact, fact)]
        # Existing edges stay active with neutral weight, and nothing hashed changed.
        cur.execute("SELECT DISTINCT is_active, weight, origin FROM memory_relations")
        assert cur.fetchall() == [(True, 1.0, None)]


def test_existing_fact_hashes_still_verify(scratch_db) -> None:
    from robothor.memory.drift import compute_fact_hash

    db, _ = scratch_db(through=_previous_migration())
    text, category = "Bob prefers tea", "preference"
    stored = compute_fact_hash(text, tenant_id=TENANT, category=category)
    with db.cursor() as cur:
        _seed_tenant(cur)
        cur.execute(
            "INSERT INTO memory_facts (fact_text, category, tenant_id, content_hash) "
            "VALUES (%s, %s, %s, %s)",
            (text, category, TENANT, stored),
        )
    scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(
            "SELECT fact_text, tenant_id, category, person_id, content_hash, scope_kind, "
            "scope_id, origin FROM memory_facts"
        )
        f_text, f_tenant, f_cat, f_person, f_hash, scope_kind, scope_id, origin = cur.fetchone()
    assert f_hash == stored
    assert compute_fact_hash(f_text, tenant_id=f_tenant, category=f_cat, person_id=f_person) == (
        stored
    )
    assert (scope_kind, scope_id, origin) == ("shared", None, None)


# ─── the code that predates the migration ────────────────────────────────


@pytest.fixture
def migrated(scratch_db, monkeypatch) -> Iterator[Any]:
    """The memory modules' connections pointed at a migrated scratch database."""
    import psycopg2

    db, dsn = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        _seed_tenant(cur)

    @contextmanager
    def _conn(*_a: Any, **_k: Any) -> Iterator[Any]:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    from robothor.memory import entities, facts

    monkeypatch.setattr(entities, "get_connection", _conn)
    monkeypatch.setattr(facts, "get_connection", _conn)

    async def _embedding(_text: str) -> list[float]:
        return [0.01] * 1024

    monkeypatch.setattr(facts.llm_client, "get_embedding_async", _embedding)
    monkeypatch.delenv("MEMORY_QUALITY_GATE", raising=False)
    yield db


async def test_the_previous_entity_and_fact_paths_run_unchanged(migrated) -> None:
    from robothor.memory.drift import compute_fact_hash
    from robothor.memory.entities import add_relation, get_entity, upsert_entity
    from robothor.memory.facts import store_fact

    fact = {
        "fact_text": "Alice works at Acme",
        "category": "personal",
        "entities": ["Alice", "Acme"],
        "confidence": 0.9,
    }
    fact_id = await store_fact(fact, "Alice said so", "conversation", tenant_id=TENANT)
    assert fact_id > 0
    alice = await upsert_entity("Alice", "person", tenant_id=TENANT)
    again = await upsert_entity("Alice", "person", tenant_id=TENANT)
    acme = await upsert_entity("Acme", "organization", tenant_id=TENANT)
    assert alice is not None and acme is not None and again == alice
    rel = await add_relation(alice, acme, "works_at", fact_id=fact_id, tenant_id=TENANT)
    assert await add_relation(alice, acme, "works_at", confidence=0.5, tenant_id=TENANT) == rel

    found = await get_entity("alice", tenant_id=TENANT)
    assert found is not None and found["id"] == alice and found["mention_count"] == 2
    assert [r["id"] for r in found["relations"]] == [rel]
    # get_entity names its columns: the 1024-float embedding never reaches the model.
    assert found["scope_kind"] == "shared" and "embedding" not in found

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT content_hash, origin, scope_kind, scope_id, event_time "
            "FROM memory_facts WHERE id = %s",
            (fact_id,),
        )
        content_hash, *rest = cur.fetchone()
        assert content_hash == compute_fact_hash(
            fact["fact_text"], tenant_id=TENANT, category=fact["category"]
        )
        assert rest == [None, "shared", None, None]
        cur.execute(
            "SELECT is_active, weight, recorded_at IS NOT NULL, predicate, retired_reason "
            "FROM memory_relations WHERE id = %s",
            (rel,),
        )
        assert cur.fetchone() == (True, 1.0, True, None, None)
