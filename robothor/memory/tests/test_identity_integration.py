"""Identity anchored to CRM records, with reversible resolution — on real Postgres.

Every test builds a scratch database from the full migration chain, so the
memory graph foundation (``memory_resolution_decisions``, ``memory_mentions``,
the CRM links on ``memory_entities``) is present and nothing touches the shared
test database other suites read.

What is proved, against the story's acceptance criteria:

* a CRM person resolves by email or full name to their own node;
* two people sharing a first name are never merged by it;
* a reversed decision returns its mentions and names the rows to rebuild;
* the backfill dry run changes nothing and reports what it would do;
* no fact of one person becomes visible to another through a resolution.

Plus seeding, decision reuse, resumability, tenant isolation, CRM hooks and
merges, the read-side lookup, and an adversarial fixture whose precision and
recall are printed (``-s``) and asserted.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg2
import pytest

from robothor.memory.identity import Ambiguity, MentionInput

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

TENANT = "test-tenant"
OTHER = "other-tenant"


def _last_migration() -> str:
    from robothor.db.migrate import _discover

    return [m.migration_id for m in _discover()][-1]


@dataclass
class World:
    db: Any
    dsn: str

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self.q(sql, params)
        return rows[0][0] if rows else None

    def count(self, table: str, tenant: str | None = None) -> int:
        if tenant is None:
            return int(self.one(f"SELECT count(*) FROM {table}"))  # noqa: S608
        return int(self.one(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (tenant,)))  # noqa: S608

    # ── CRM rows ──
    def company(self, name: str, domain: str | None = None, tenant: str = TENANT) -> str:
        cid = str(uuid.uuid4())
        self.q(
            "INSERT INTO crm_companies (id, name, domain_name, tenant_id) VALUES (%s, %s, %s, %s)",
            (cid, name, domain, tenant),
        )
        return cid

    def person(
        self,
        first: str,
        last: str = "",
        email: str | None = None,
        *,
        extra_emails: tuple[str, ...] = (),
        company: str | None = None,
        tenant: str = TENANT,
    ) -> str:
        pid = str(uuid.uuid4())
        self.q(
            "INSERT INTO crm_people (id, first_name, last_name, email, additional_emails, "
            "company_id, tenant_id) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
            (pid, first, last, email, json.dumps(list(extra_emails)), company, tenant),
        )
        return pid

    def channel(self, person_id: str, channel: str, ident: str, tenant: str = TENANT) -> None:
        self.q(
            "INSERT INTO contact_identifiers (channel, identifier, person_id, tenant_id) "
            "VALUES (%s, %s, %s, %s)",
            (channel, ident, person_id, tenant),
        )

    def owner(self, person_id: str, tenant: str = TENANT) -> None:
        self.q(
            "INSERT INTO tenant_users (display_name, tenant_id, role, person_id) "
            "VALUES ('Operator', %s, 'owner', %s)",
            (tenant, person_id),
        )

    # ── memory rows ──
    def entity(self, name: str, etype: str = "person", tenant: str = TENANT) -> int:
        return int(
            self.one(
                "INSERT INTO memory_entities (name, entity_type, tenant_id) VALUES (%s, %s, %s) "
                "RETURNING id",
                (name, etype, tenant),
            )
        )

    def fact(
        self,
        text: str,
        names: list[str],
        *,
        session: str | None = None,
        person_id: str | None = None,
        scope: tuple[str, str | None] = ("shared", None),
        tenant: str = TENANT,
    ) -> int:
        meta = {"session_key": session} if session else {}
        return int(
            self.one(
                "INSERT INTO memory_facts (fact_text, category, entities, metadata, person_id, "
                "scope_kind, scope_id, tenant_id) VALUES (%s, 'personal', %s, %s::jsonb, %s, "
                "%s, %s, %s) RETURNING id",
                (text, names, json.dumps(meta), person_id, scope[0], scope[1], tenant),
            )
        )

    def entity_of(self, crm_id: str) -> int | None:
        return self.one(
            "SELECT id FROM memory_entities WHERE crm_person_id = %s OR crm_company_id = %s "
            "ORDER BY id LIMIT 1",
            (crm_id, crm_id),
        )

    def snapshot(self) -> dict[str, Any]:
        """Everything a dry run must leave exactly as it was, sequences included."""
        out: dict[str, Any] = {}
        for t in ("memory_entities", "memory_resolution_decisions", "memory_mentions"):
            out[t] = self.q(f"SELECT * FROM {t} ORDER BY 1")  # noqa: S608
        out["facts"] = self.q(
            "SELECT id, person_id, scope_kind, scope_id, entities FROM memory_facts ORDER BY id"
        )
        for seq in ("memory_entities_id_seq", "memory_resolution_decisions_id_seq",
                    "memory_mentions_id_seq"):  # fmt: skip
            out[seq] = self.one(f"SELECT last_value FROM {seq}")  # noqa: S608
        return out


@pytest.fixture
def world(scratch_db, monkeypatch) -> Iterator[World]:
    db, dsn = scratch_db(through=_last_migration())
    w = World(db, dsn)
    for tenant in (TENANT, OTHER):
        w.q(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Test') ON CONFLICT DO NOTHING",
            (tenant,),
        )

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

    from robothor.crm import dal
    from robothor.memory import entities, facts, identity

    for module in (identity, entities, facts, dal):
        monkeypatch.setattr(module, "get_connection", _conn)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    yield w


def _resolve(*mentions: MentionInput, tenant: str = TENANT) -> list[Any]:
    from robothor.memory.identity import resolve_mentions

    return resolve_mentions(tenant, list(mentions))


def _one(mention: MentionInput, tenant: str = TENANT) -> Any:
    return _resolve(mention, tenant=tenant)[0]


# ═══ 1. CRM seeding ══════════════════════════════════════════════════════


def test_every_crm_person_and_company_gets_exactly_one_linked_node(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    acme = world.company("Northwind Traders", "northwind.example")
    dana = world.person("Dana", "Reyes", "Dana@Example.com", extra_emails=("dr@example.org",),
                        company=acme)  # fmt: skip
    bob = world.person("Robert", "Rivera", "robert@initech.example")

    report = sync_crm_entities(TENANT)
    assert report["people"]["created"] == 2 and report["companies"]["created"] == 1

    rows = world.q(
        "SELECT name, entity_type, aliases, crm_person_id::text, crm_company_id::text "
        "FROM memory_entities WHERE tenant_id = %s ORDER BY name",
        (TENANT,),
    )
    by_name = {r[0]: r for r in rows}
    assert set(by_name) == {"Dana Reyes", "Northwind Traders", "Robert Rivera"}
    assert by_name["Dana Reyes"][1:2] == ("person",) and by_name["Dana Reyes"][3] == dana
    # CRM contact data (emails, domains) is never copied into memory: get_entity
    # applies no data scope, and the CRM withholds it from restricted readers.
    assert by_name["Dana Reyes"][2] == []
    assert by_name["Northwind Traders"][1] == "organization"
    assert by_name["Northwind Traders"][4] == acme
    assert by_name["Northwind Traders"][2] == []
    # Nickname variants are never written as aliases: the CRM does not hold "Bob".
    assert by_name["Robert Rivera"][3] == bob
    assert not {"bob", "bob rivera", "rob rivera"} & {
        a.lower() for a in by_name["Robert Rivera"][2]
    }

    again = sync_crm_entities(TENANT)
    assert again["people"] == {
        "created": 0,
        "linked": 0,
        "updated": 0,
        "unchanged": 2,
        "skipped": 0,
    }
    assert again["companies"]["unchanged"] == 1
    assert world.count("memory_entities", TENANT) == 3


def test_an_unlinked_node_with_the_same_full_name_is_linked_not_duplicated(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    legacy = world.entity("Dana Reyes", "person")
    dana = world.person("Dana", "Reyes", "dana@example.com")
    report = sync_crm_entities(TENANT)
    assert report["people"]["linked"] == 1 and report["people"]["created"] == 0
    assert world.entity_of(dana) == legacy
    assert world.count("memory_entities", TENANT) == 1
    method, entity = world.q(
        "SELECT method, resolved_entity_id FROM memory_resolution_decisions WHERE tenant_id = %s",
        (TENANT,),
    )[0]
    assert (method, entity) == ("exact", legacy)


def test_two_crm_people_with_one_name_get_two_nodes_and_no_legacy_link(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    legacy = world.entity("Alex Morgan", "person")
    a = world.person("Alex", "Morgan", "alex@northwind.example")
    b = world.person("Alex", "Morgan", "amorgan@globex.example")
    sync_crm_entities(TENANT)
    ea, eb = world.entity_of(a), world.entity_of(b)
    assert ea and eb and len({ea, eb, legacy}) == 3
    # Both are findable by the full name, so a bare "Alex Morgan" is ambiguous.
    out = _one(MentionInput("Alex Morgan", "person"))
    assert out.entity_id is None


def test_sync_dry_run_writes_nothing(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    world.entity("Dana Reyes", "person")
    world.person("Dana", "Reyes", "dana@example.com")
    world.company("Globex")
    before = world.snapshot()
    report = sync_crm_entities(TENANT, dry_run=True)
    assert report["dry_run"] is True
    assert report["people"]["linked"] == 1 and report["companies"]["created"] == 1
    assert world.snapshot() == before


def test_a_crm_rename_follows_on_the_linked_node(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    pid = world.person("Dana", "Reyes", "dana@example.com")
    sync_crm_entities(TENANT)
    node = world.entity_of(pid)
    world.q("UPDATE crm_people SET last_name = 'Ruiz', email = 'druiz@example.com' WHERE id = %s",
            (pid,))  # fmt: skip
    assert sync_crm_entities(TENANT)["people"]["updated"] == 1
    name, aliases = world.q("SELECT name, aliases FROM memory_entities WHERE id = %s", (node,))[0]
    assert name == "Dana Ruiz"
    # The old name stays findable: facts already name the person by it.
    assert aliases == ["Dana Reyes"]
    assert sync_crm_entities(TENANT)["people"]["unchanged"] == 1
    from robothor.memory.identity import find_entity

    found = find_entity(TENANT, "Dana Reyes")
    assert isinstance(found, dict) and found["id"] == node
    assert _one(MentionInput("Dana Reyes", "person")).entity_id == node
    assert _one(MentionInput("Dana Ruiz", "person")).entity_id == node


# ═══ 2. resolution — acceptance criterion 1 ══════════════════════════════


def test_ac1_a_crm_person_resolves_by_email_and_by_full_name(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    pid = world.person("Dana", "Reyes", "dana@example.com")
    sync_crm_entities(TENANT)
    node = world.entity_of(pid)

    by_email = _one(MentionInput("D. R.", "person", strong_keys={"email": "DANA@example.com"}))
    assert (by_email.entity_id, by_email.method, by_email.confidence) == (node, "strong_key", 1.0)
    assert by_email.canonical_name == "Dana Reyes"

    by_name = _one(MentionInput("Dana Reyes", "person"))
    assert (by_name.entity_id, by_name.method) == (node, "full_name")

    for variant in ("dana reyes", "  DANA   Reyes "):
        assert _one(MentionInput(variant, "person")).entity_id == node

    # An email written as the surface form is a strong key too.
    assert _one(MentionInput("dana@example.com", None)).entity_id == node


def test_a_crm_person_not_yet_seeded_is_linked_on_first_strong_key(world: World) -> None:
    pid = world.person("Dana", "Reyes", "dana@example.com")
    out = _one(MentionInput("Dana", "person", strong_keys={"crm_person_id": pid}))
    assert out.method == "strong_key" and out.entity_id == world.entity_of(pid)


def test_a_channel_identifier_is_a_strong_key(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    pid = world.person("Robert", "Rivera", "robert@initech.example")
    world.channel(pid, "telegram", "4242")
    sync_crm_entities(TENANT)
    out = _one(MentionInput("Ro", "person", strong_keys={"channel": "telegram:4242"}))
    assert (out.entity_id, out.method) == (world.entity_of(pid), "strong_key")


def test_an_email_of_another_person_wins_over_the_name(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    world.person("Dana", "Reyes", "dana@example.com")
    bob = world.person("Bob", "Okafor", "bob.okafor@example.com")
    sync_crm_entities(TENANT)
    out = _one(
        MentionInput("Dana Reyes", "person", strong_keys={"email": "bob.okafor@example.com"})
    )
    assert (out.entity_id, out.method) == (world.entity_of(bob), "strong_key")


# ═══ 2. resolution — acceptance criterion 2 ══════════════════════════════


def test_ac2_two_people_sharing_a_first_name_are_never_merged(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    a = world.person("Alex", "Morgan", "alex@northwind.example")
    c = world.person("Alex", "Chen", "alex.chen@globex.example")
    sync_crm_entities(TENANT)
    entities_before = world.count("memory_entities", TENANT)

    out = _one(MentionInput("Alex", "person"))
    assert out.entity_id is None
    decision = world.q(
        "SELECT resolved_entity_id, method, evidence FROM memory_resolution_decisions WHERE id = %s",
        (out.decision_id,),
    )[0]
    assert decision[0] is None and decision[1] == out.method
    assert sorted(decision[2]["candidates"]) == sorted([world.entity_of(a), world.entity_of(c)])
    assert world.count("memory_entities", TENANT) == entities_before  # nothing created


def test_ac2_a_first_name_resolves_by_conversation_context_with_a_recorded_method(
    world: World,
) -> None:
    from robothor.memory.identity import resolve_mentions, sync_crm_entities, write_mentions

    world.person("Alex", "Morgan", "alex@northwind.example")
    c = world.person("Alex", "Chen", "alex.chen@globex.example")
    sync_crm_entities(TENANT)
    earlier = world.fact("Alex Chen joined the call", ["Alex Chen"], session="s-1")
    resolved = resolve_mentions(TENANT, [MentionInput("Alex Chen", "person")])
    write_mentions(TENANT, earlier, [MentionInput("Alex Chen", "person")], resolved)

    out = _one(MentionInput("Alex", "person", context={"conversation_ref": "s-1"}))
    assert (out.entity_id, out.method) == (world.entity_of(c), "context")
    assert out.confidence < 1
    # A different conversation gives no such evidence.
    assert (
        _one(MentionInput("Alex", "person", context={"conversation_ref": "s-2"})).entity_id is None
    )


def test_a_nickname_shared_by_two_people_is_never_merged(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    world.person("Samantha", "Lee", "sam.lee@example.com")
    world.person("Samantha", "Cole", "sam.cole@example.com")
    sync_crm_entities(TENANT)
    assert _one(MentionInput("Sam", "person")).entity_id is None
    assert _one(MentionInput("Sam", None)).entity_id is None


def test_a_nickname_full_name_resolves_only_with_context(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    initech = world.company("Initech")
    pid = world.person("Robert", "Rivera", "robert@initech.example", company=initech)
    sync_crm_entities(TENANT)
    assert _one(MentionInput("Bob Rivera", "person")).entity_id is None
    out = _one(MentionInput("Bob Rivera", "person", context={"organisation": "initech"}))
    assert (out.entity_id, out.method) == (world.entity_of(pid), "context")
    out = _one(MentionInput("Bob Rivera", "person", context={"email_domain": "initech.example"}))
    assert out.entity_id == world.entity_of(pid)
    # No provisional "Bob Rivera" node was made beside Robert.
    assert world.one("SELECT count(*) FROM memory_entities WHERE name = 'Bob Rivera'") == 0


def test_a_person_and_an_organisation_with_one_name_stay_apart(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    org = world.company("Jordan Lee")
    pid = world.person("Jordan", "Lee", "jordan@example.com")
    sync_crm_entities(TENANT)
    assert _one(MentionInput("Jordan Lee", "person")).entity_id == world.entity_of(pid)
    assert _one(MentionInput("Jordan Lee", "organization")).entity_id == world.entity_of(org)
    assert _one(MentionInput("Jordan Lee", None)).entity_id is None


def test_an_unknown_full_name_becomes_one_provisional_node(world: World) -> None:
    first = _one(MentionInput("Priya Natarajan", "person"))
    again = _one(MentionInput("priya natarajan", "person"))
    assert first.entity_id is not None and first.method == "exact"
    assert again.entity_id == first.entity_id and again.method == "full_name"
    row = world.q(
        "SELECT name, entity_type, crm_person_id FROM memory_entities WHERE id = %s",
        (first.entity_id,),
    )[0]
    assert row == ("Priya Natarajan", "person", None)
    assert _one(MentionInput("Quill", "person")).entity_id is None
    assert world.one("SELECT count(*) FROM memory_entities WHERE name = 'Quill'") == 0


# ═══ the operator ════════════════════════════════════════════════════════


def test_the_operator_resolves_by_strong_key_and_never_by_first_name(world: World) -> None:
    from robothor.memory.identity import resolve_mentions, sync_crm_entities, write_mentions

    op = world.person("Casey", "Quinn", "casey@example.com")
    world.owner(op)
    sync_crm_entities(TENANT)
    node = world.entity_of(op)
    out = _one(MentionInput("me", "person", strong_keys={"crm_person_id": op}))
    assert out.entity_id == node

    # Even with the operator evidenced in this very conversation, "Casey" is not them.
    fid = world.fact("Casey Quinn asked for the report", ["Casey Quinn"], session="s-op")
    resolved = resolve_mentions(TENANT, [MentionInput("Casey Quinn", "person")])
    write_mentions(TENANT, fid, [MentionInput("Casey Quinn", "person")], resolved)
    out = _one(MentionInput("Casey", "person", context={"conversation_ref": "s-op"}))
    assert out.entity_id is None


# ═══ decisions are recorded, and reused ══════════════════════════════════


def test_every_mention_records_a_decision_and_identical_ones_are_reused(world: World) -> None:
    from robothor.memory.identity import sync_crm_entities

    world.person("Dana", "Reyes", "dana@example.com")
    world.person("Alex", "Morgan")
    world.person("Alex", "Chen")
    sync_crm_entities(TENANT)
    seeded = world.count("memory_resolution_decisions", TENANT)

    batch = [
        MentionInput("Dana Reyes", "person"),
        MentionInput("Alex", "person"),
        MentionInput("Dana Reyes", "person", strong_keys={"email": "dana@example.com"}),
    ]
    first = _resolve(*batch)
    assert len({r.decision_id for r in first}) == 3
    grown = world.count("memory_resolution_decisions", TENANT)
    assert grown == seeded + 3

    for _ in range(3):  # re-ingesting the same text
        again = _resolve(*batch)
        assert [r.decision_id for r in again] == [r.decision_id for r in first]
    assert _one(MentionInput("dana  REYES", "person")).decision_id == first[0].decision_id
    assert world.count("memory_resolution_decisions", TENANT) == grown


def test_resolution_inside_a_callers_transaction_is_undone_with_it(world: World) -> None:
    from robothor.memory.identity import resolve_mentions

    before = world.snapshot()
    conn = psycopg2.connect(world.dsn)
    try:
        cur = conn.cursor()
        out = resolve_mentions(TENANT, [MentionInput("Priya Natarajan", "person")], cur=cur)
        assert out[0].entity_id is not None
        conn.rollback()
    finally:
        conn.close()
    after = world.snapshot()
    for key in ("memory_entities", "memory_resolution_decisions", "memory_mentions"):
        assert after[key] == before[key]


# ═══ 3. reversal — acceptance criterion 3 ════════════════════════════════


def _wrongly_resolved(world: World) -> dict[str, Any]:
    """'Robert Rivera' resolved to the provisional node of a namesake, then used in facts."""
    from robothor.memory.identity import resolve_mentions, sync_crm_entities, write_mentions

    wrong = world.entity("Robert Rivera", "person")  # a namesake the old graph created
    right_pid = world.person("Bob", "Rivera", "bob.rivera@example.com")
    sync_crm_entities(TENANT)
    right = world.entity_of(right_pid)
    globex = world.entity("Globex", "organization")
    m = MentionInput("Robert Rivera", "person")
    facts = [world.fact(f"Robert Rivera note {i}", ["Robert Rivera", "Globex"]) for i in range(2)]
    other = world.fact("Globex ships widgets", ["Globex"])
    decision = None
    for fid in facts:
        out = resolve_mentions(TENANT, [m])
        decision = out[0].decision_id
        assert out[0].entity_id == wrong
        write_mentions(TENANT, fid, [m], out)
    rel = world.one(
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "tenant_id) VALUES (%s, %s, 'works at', %s) RETURNING id",
        (wrong, globex, TENANT),
    )
    unrelated = world.one(
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "tenant_id) VALUES (%s, %s, 'competes with', %s) RETURNING id",
        (globex, globex, TENANT),
    )
    for fid in facts:
        world.q(
            "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
            "VALUES (%s, %s, %s)",
            (TENANT, rel, fid),
        )
    world.q(
        "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) VALUES (%s, %s, %s)",
        (TENANT, unrelated, other),
    )
    return {"wrong": wrong, "right": right, "decision": decision, "facts": facts, "rel": rel,
            "unrelated": unrelated, "mention": m}  # fmt: skip


def test_ac3_reversing_a_wrong_resolution_repoints_mentions_and_names_the_rebuild(
    world: World,
) -> None:
    from robothor.memory.identity import reverse_decision

    w = _wrongly_resolved(world)
    entities_before = world.q("SELECT * FROM memory_entities ORDER BY id")
    report = reverse_decision(TENANT, w["decision"], reason="different person",
                              new_entity_id=w["right"])  # fmt: skip

    assert sorted(report["fact_ids"]) == sorted(w["facts"])
    assert report["relation_ids"] == [w["rel"]]
    new_decision = report["new_decision_id"]
    assert new_decision and new_decision != w["decision"]

    old = world.q(
        "SELECT reversed_at IS NOT NULL, superseded_by, evidence FROM memory_resolution_decisions "
        "WHERE id = %s",
        (w["decision"],),
    )[0]
    assert old[0] is True and old[1] == new_decision
    # The rebuild set is durable on the reversed decision, not only in the return value.
    assert sorted(old[2]["reversal"]["rebuild"]["relation_ids"]) == [w["rel"]]
    method, entity = world.q(
        "SELECT method, resolved_entity_id FROM memory_resolution_decisions WHERE id = %s",
        (new_decision,),
    )[0]
    assert (method, entity) == ("operator", w["right"])

    mentions = world.q(
        "SELECT fact_id, entity_id, decision_id FROM memory_mentions WHERE surface_form = %s "
        "ORDER BY fact_id",
        ("Robert Rivera",),
    )
    assert mentions == [(f, w["right"], new_decision) for f in sorted(w["facts"])]
    # Entity rows are neither rewritten nor deleted.
    assert world.q("SELECT * FROM memory_entities ORDER BY id") == entities_before

    # Re-ingesting the same text now follows the operator, not the wrong rule.
    again = _one(w["mention"])
    assert (again.entity_id, again.decision_id) == (w["right"], new_decision)


def test_reversing_without_a_replacement_leaves_the_mentions_unresolved(world: World) -> None:
    from robothor.memory.identity import reverse_decision

    w = _wrongly_resolved(world)
    report = reverse_decision(TENANT, w["decision"], reason="not sure who this is")
    assert report["new_decision_id"] is None
    assert world.q(
        "SELECT DISTINCT entity_id FROM memory_mentions WHERE surface_form = 'Robert Rivera'"
    ) == [(None,)]
    # The rejected rule is not re-applied on the next ingest.
    again = _one(w["mention"])
    assert again.entity_id is None and again.decision_id != w["decision"]
    with pytest.raises(ValueError):
        reverse_decision(TENANT, w["decision"], reason="twice")


def test_a_decision_of_another_tenant_cannot_be_reversed(world: World) -> None:
    from robothor.memory.identity import reverse_decision

    w = _wrongly_resolved(world)
    with pytest.raises(LookupError):
        reverse_decision(OTHER, w["decision"], reason="not mine")
    other_node = world.entity("Robert Rivera", "person", tenant=OTHER)
    with pytest.raises(LookupError):
        reverse_decision(TENANT, w["decision"], reason="x", new_entity_id=other_node)


# ═══ CRM hooks and merges ════════════════════════════════════════════════


def test_crm_writes_through_the_dal_keep_the_link_current(world: World) -> None:
    from robothor.crm import dal

    pid = dal.create_person("Priya", "Natarajan", email="priya@example.com", tenant_id=TENANT)
    assert pid
    node = world.entity_of(pid)
    assert node and world.one("SELECT name FROM memory_entities WHERE id = %s", (node,)) == (
        "Priya Natarajan"
    )
    assert dal.update_person(pid, TENANT, last_name="Shah")
    assert world.one("SELECT name FROM memory_entities WHERE id = %s", (node,)) == "Priya Shah"

    cid = dal.create_company("Initrode", domain_name="initrode.example", tenant_id=TENANT)
    assert cid and world.entity_of(cid)
    assert dal.update_company(cid, TENANT, name="Initrode Global")
    assert world.one("SELECT name FROM memory_entities WHERE crm_company_id = %s", (cid,)) == (
        "Initrode Global"
    )
    # Memory never writes to the CRM.
    assert world.one("SELECT last_name FROM crm_people WHERE id = %s", (pid,)) == "Shah"


def test_a_crm_link_failure_never_fails_the_crm_write(world: World, monkeypatch) -> None:
    from robothor.crm import dal, hooks

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("memory is down")

    monkeypatch.setattr(hooks, "_crm_entity_sync", _boom)
    assert dal.create_person("Priya", "Natarajan", tenant_id=TENANT)


def test_a_crm_merge_resolves_the_losers_node_to_the_winner_without_rewriting(
    world: World,
) -> None:
    from robothor.crm import dal
    from robothor.memory.identity import (
        resolve_mentions,
        reverse_decision,
        sync_crm_entities,
        write_mentions,
    )

    keeper = world.person("Dana", "Reyes", "dana@example.com")
    loser = world.person("Danielle", "Reyes", "danielle@example.org")
    sync_crm_entities(TENANT)
    keeper_node, loser_node = world.entity_of(keeper), world.entity_of(loser)
    fid = world.fact("Danielle Reyes signed", ["Danielle Reyes"])
    m = MentionInput("Danielle Reyes", "person")
    write_mentions(TENANT, fid, [m], resolve_mentions(TENANT, [m]))

    assert dal.merge_people(keeper, loser, TENANT)

    # The mention still carries the loser's node: ids are not rewritten.
    assert world.q("SELECT entity_id FROM memory_mentions WHERE fact_id = %s", (fid,)) == [
        (loser_node,)
    ]
    merge = world.q(
        "SELECT id, surface_form, resolved_entity_id, method FROM memory_resolution_decisions "
        "WHERE evidence->>'basis' = %s",
        (f"crm_merge:person:{loser}",),
    )
    assert len(merge) == 1 and merge[0][2] == keeper_node
    # New mentions of the loser follow the merge.
    assert _one(MentionInput("x", "person", strong_keys={"crm_person_id": loser})).entity_id == (
        keeper_node
    )
    assert _one(m).entity_id == keeper_node
    assert _one(
        MentionInput("D", "person", strong_keys={"email": "danielle@example.org"})
    ).entity_id == (keeper_node)

    # A merge is a decision like any other, and can be undone.
    reverse_decision(TENANT, merge[0][0], reason="not the same person")
    assert _one(m).entity_id is None  # its CRM record is gone and no merge stands
    assert world.one("SELECT count(*) FROM memory_entities WHERE name = 'Danielle Reyes'") == 1


# ═══ 4. backfill ═════════════════════════════════════════════════════════


def _backfill_world(world: World) -> dict[str, Any]:
    from robothor.memory.identity import sync_crm_entities

    globex = world.company("Globex", "globex.example")
    world.person("Dana", "Reyes", "dana@example.com")
    world.person("Alex", "Morgan", "alex@northwind.example")
    g = world.person("Alex", "Morgan", "amorgan@globex.example", company=globex)
    world.person("Alex", "Chen", "alex.chen@globex.example", company=globex)
    sync_crm_entities(TENANT)
    world.entity("Alex", "person")  # a legacy single-token node
    ids = {
        "dana": world.fact("Dana Reyes visited Globex", ["Dana Reyes", "Globex"]),
        "alex": world.fact("Alex called", ["Alex"]),
        "chen": world.fact("Alex Chen demoed", ["Alex Chen"], session="s-9"),
        "alex_s9": world.fact("Alex followed up", ["Alex"], session="s-9"),
        "morgan": world.fact("Alex Morgan said hi", ["Alex Morgan"]),
        "morgan_g": world.fact("Alex Morgan at Globex", ["Alex Morgan", "Globex"]),
        "morgan_pid": world.fact("Alex Morgan prefers tea", ["Alex Morgan"], person_id=g),
        "dup": world.fact("Dana Reyes and dana reyes", ["Dana Reyes", "dana reyes"]),
    }
    world.q("UPDATE memory_facts SET is_active = FALSE WHERE id = %s", (ids["dup"],))
    ids["inactive"] = ids.pop("dup")
    ids["g_node"] = world.entity_of(g)
    return ids


def test_ac4_backfill_dry_run_reports_and_changes_nothing(world: World) -> None:
    from robothor.memory.identity import backfill_mentions

    ids = _backfill_world(world)
    before = world.snapshot()
    report = backfill_mentions(TENANT, dry_run=True)
    assert world.snapshot() == before

    assert report["dry_run"] is True and report["mentions_written"] == 0
    assert report["facts_scanned"] == 7  # the inactive fact is not walked
    by = report["by_method"]
    assert by["full_name"] >= 2 and by["strong_key"] == 1 and by["context"] >= 2
    assert by["unresolved"] >= 2
    unresolved = {u["surface_form"]: u for u in report["unresolved"]}
    assert "Alex" in unresolved and len(unresolved["Alex"]["candidates"]) >= 3
    assert all({"id", "name", "entity_type"} <= set(c) for c in unresolved["Alex"]["candidates"])
    assert "Alex Morgan" in unresolved
    assert report["provisional"] == []
    for res in report["resolutions"]:
        assert res["method"] in {"full_name", "context"}
        assert res["evidence"]["basis"]
    ctx = [r for r in report["resolutions"] if r["method"] == "context"]
    assert {r["fact_id"] for r in ctx} >= {ids["alex_s9"], ids["morgan_g"]}
    assert report["done"] is True and report["last_fact_id"] == max(
        v for k, v in ids.items() if k not in {"g_node", "inactive"}
    )


def test_backfill_writes_what_the_dry_run_predicted_and_is_idempotent(world: World) -> None:
    from robothor.memory.identity import backfill_mentions

    ids = _backfill_world(world)
    planned = backfill_mentions(TENANT, dry_run=True)
    real = backfill_mentions(TENANT, dry_run=False)
    assert real["by_method"] == planned["by_method"]
    assert real["mentions_written"] == planned["mentions_planned"] == 9
    assert world.count("memory_mentions", TENANT) == 9

    rows = dict(
        world.q(
            "SELECT m.fact_id, m.entity_id FROM memory_mentions m WHERE m.surface_form = "
            "'Alex Morgan' AND m.fact_id IN (%s, %s)",
            (ids["morgan_g"], ids["morgan_pid"]),
        )
    )
    # Globex named in the same fact, and the fact's own person scope, both single out G.
    assert rows == {ids["morgan_g"]: ids["g_node"], ids["morgan_pid"]: ids["g_node"]}
    s9 = world.one("SELECT entity_id FROM memory_mentions WHERE fact_id = %s", (ids["alex_s9"],))
    assert s9 is not None and s9 == world.one(
        "SELECT entity_id FROM memory_mentions WHERE fact_id = %s", (ids["chen"],)
    )
    assert (
        world.one("SELECT entity_id FROM memory_mentions WHERE fact_id = %s", (ids["alex"],))
        is None
    )

    decisions = world.count("memory_resolution_decisions", TENANT)
    again = backfill_mentions(TENANT, dry_run=False)
    assert again["mentions_written"] == 0 and again["skipped_existing"] == 9
    assert world.count("memory_mentions", TENANT) == 9
    assert world.count("memory_resolution_decisions", TENANT) == decisions


def test_backfill_is_resumable_in_bounded_batches(world: World) -> None:
    from robothor.memory.identity import backfill_mentions

    _backfill_world(world)
    first = backfill_mentions(TENANT, dry_run=False, batch_size=2, max_batches=1)
    assert first["done"] is False and first["batches"] == 1 and first["facts_scanned"] == 2
    cursor = first["last_fact_id"]
    seen = first["facts_scanned"]
    while True:
        step = backfill_mentions(TENANT, dry_run=False, batch_size=2, max_batches=1,
                                 resume_from=cursor)  # fmt: skip
        seen += step["facts_scanned"]
        assert step["last_fact_id"] >= cursor
        cursor = step["last_fact_id"]
        if step["done"]:
            break
    assert seen == 7
    assert world.count("memory_mentions", TENANT) == 9


# ═══ tenant isolation ════════════════════════════════════════════════════


def test_two_tenants_sharing_names_and_emails_never_cross(world: World) -> None:
    from robothor.memory.identity import backfill_mentions, sync_crm_entities

    mine = world.person("Dana", "Reyes", "dana@example.com")
    theirs = world.person("Dana", "Reyes", "dana@example.com", tenant=OTHER)
    world.person("Alex", "Chen", tenant=OTHER)
    sync_crm_entities(TENANT)
    sync_crm_entities(OTHER)
    my_node, their_node = world.entity_of(mine), world.entity_of(theirs)
    assert my_node != their_node

    assert _one(MentionInput("Dana Reyes", "person")).entity_id == my_node
    assert _one(MentionInput("Dana Reyes", "person"), tenant=OTHER).entity_id == their_node
    by_email = _one(MentionInput("D", "person", strong_keys={"email": "dana@example.com"}))
    assert by_email.entity_id == my_node
    # Another tenant's CRM id is no key here; the name still resolves in-tenant.
    out = _one(MentionInput("Dana Reyes", "person", strong_keys={"crm_person_id": theirs}))
    assert out.entity_id == my_node and out.method != "strong_key"
    # A name that exists only in the other tenant is not found here.
    alex = _one(MentionInput("Alex Chen", "person"))
    assert alex.entity_id != world.entity_of(world.one(
        "SELECT id::text FROM crm_people WHERE tenant_id = %s AND first_name = 'Alex'", (OTHER,)
    ))  # fmt: skip

    world.fact("Dana Reyes visited", ["Dana Reyes"])
    other_before = {
        t: world.q(f"SELECT * FROM {t} WHERE tenant_id = %s ORDER BY 1", (OTHER,))  # noqa: S608
        for t in ("memory_entities", "memory_resolution_decisions", "memory_mentions")
    }
    backfill_mentions(TENANT, dry_run=False)
    for t, rows in other_before.items():
        assert world.q(f"SELECT * FROM {t} WHERE tenant_id = %s ORDER BY 1", (OTHER,)) == rows  # noqa: S608
    for t in ("memory_resolution_decisions", "memory_mentions"):
        rows = world.q(f"SELECT DISTINCT tenant_id FROM {t}")  # noqa: S608
        assert rows in ([(TENANT,)], [(TENANT,), (OTHER,)], [(OTHER,), (TENANT,)])
    # Every decision of TENANT points only at TENANT nodes.
    assert (
        world.one(
            "SELECT count(*) FROM memory_resolution_decisions d JOIN memory_entities e "
            "ON e.id = d.resolved_entity_id WHERE d.tenant_id <> e.tenant_id"
        )
        == 0
    )


# ═══ 5. privacy — acceptance criterion 5 ═════════════════════════════════


def _conversation_with(world: World, *, scope: tuple[str, str | None], person_id: str | None
                       ) -> int:  # fmt: skip
    from robothor.memory.identity import resolve_mentions, sync_crm_entities, write_mentions

    world.person("Alex", "Morgan", "alex@northwind.example")
    c = world.person("Alex", "Chen", "alex.chen@globex.example")
    sync_crm_entities(TENANT)
    fid = world.fact("Alex Chen shared a secret", ["Alex Chen"], session="s-p",
                     scope=scope, person_id=person_id)  # fmt: skip
    m = MentionInput("Alex Chen", "person")
    write_mentions(TENANT, fid, [m], resolve_mentions(TENANT, [m]))
    return int(world.entity_of(c))


def test_a_user_private_fact_of_someone_else_is_not_context(world: World) -> None:
    chen = _conversation_with(world, scope=("user", "u-other"), person_id=None)
    mine = {"conversation_ref": "s-p", "scope_kind": "user", "scope_id": "u-me"}
    assert _one(MentionInput("Alex", "person", context=mine)).entity_id is None
    shared = {"conversation_ref": "s-p"}
    assert _one(MentionInput("Alex", "person", context=shared)).entity_id is None
    owner = {"conversation_ref": "s-p", "scope_kind": "user", "scope_id": "u-other"}
    assert _one(MentionInput("Alex", "person", context=owner)).entity_id == chen


def test_a_person_scoped_fact_of_someone_else_is_not_context(world: World) -> None:
    someone = world.person("Morgan", "Blake", "morgan.blake@example.com")
    chen = _conversation_with(world, scope=("shared", None), person_id=someone)
    assert (
        _one(MentionInput("Alex", "person", context={"conversation_ref": "s-p"})).entity_id is None
    )
    theirs = {"conversation_ref": "s-p", "person_id": someone}
    assert _one(MentionInput("Alex", "person", context=theirs)).entity_id == chen


async def test_ac5_no_fact_of_another_person_becomes_visible_through_a_merge(
    world: World, monkeypatch
) -> None:
    from robothor.crm import dal
    from robothor.identity.scope import DataScope
    from robothor.memory import facts as facts_mod
    from robothor.memory.identity import backfill_mentions, sync_crm_entities

    async def _embedding(_text: str) -> list[float]:
        return [0.01] * 1024

    monkeypatch.setattr(facts_mod.llm_client, "get_embedding_async", _embedding)
    monkeypatch.delenv("MEMORY_QUALITY_GATE", raising=False)

    a = world.person("Dana", "Reyes", "dana@example.com")
    b = world.person("Dana", "Reyes", "dana.r@example.org")
    reader = world.person("Riley", "Stone", "riley@example.com")
    sync_crm_entities(TENANT)
    # A's private fact hangs off a node that A's entity has an edge to, so the
    # entity-graph expansion of a search for the shared fact walks right past it.
    bank = world.entity("Northbank", "organization")
    world.q(
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "tenant_id) VALUES (%s, %s, 'banks with', %s)",
        (world.entity_of(a), bank, TENANT),
    )
    private = world.fact("Northbank holds a loan for Dana", ["Northbank"], person_id=a)
    shared = world.fact("Dana Reyes likes the bank holiday", ["Dana Reyes"])
    world.q(
        "UPDATE memory_facts SET embedding = %s::vector, importance_score = 0.9",
        (str([0.01] * 1024),),
    )
    scope = DataScope(tenant_id=TENANT, person_id=reader, restricted=True)

    async def _visible() -> set[int]:
        rows = await facts_mod.search_facts(
            "Dana Reyes bank", tenant_id=TENANT, scope=scope, use_reranker=False,
            expand_entities=True,
        )  # fmt: skip
        return {int(r["id"]) for r in rows}

    before_rows = world.q(
        "SELECT id, person_id, scope_kind, scope_id FROM memory_facts ORDER BY id"
    )
    before = await _visible()
    assert private not in before and shared in before

    backfill_mentions(TENANT, dry_run=False)
    assert dal.merge_people(a, b, TENANT)  # the two Danas are merged in the CRM
    backfill_mentions(TENANT, dry_run=False)

    assert await _visible() == before
    # This module never stamps or moves a fact's person scope.
    assert world.q("SELECT id, person_id, scope_kind, scope_id FROM memory_facts ORDER BY id") == (
        before_rows
    )


# ═══ 6. read side ════════════════════════════════════════════════════════


async def test_find_entity_is_a_pure_lookup_and_get_entity_keeps_its_shape(world: World) -> None:
    from robothor.memory.entities import get_entity
    from robothor.memory.identity import find_entity, sync_crm_entities

    pid = world.person("Dana", "Reyes", "dana@example.com")
    a = world.person("Alex", "Morgan")
    world.person("Alex", "Morgan")
    sync_crm_entities(TENANT)
    node = world.entity_of(pid)
    before = world.snapshot()

    # An email is not a read-side key: it would tell any reader who owns an address.
    assert find_entity(TENANT, "dana@example.com") is None
    found = find_entity(TENANT, "Dana Reyes")
    assert isinstance(found, dict) and found["id"] == node and "embedding" not in found
    again = find_entity(TENANT, "  dana REYES")
    assert isinstance(again, dict) and again["id"] == node
    amb = find_entity(TENANT, "Alex Morgan")
    assert isinstance(amb, Ambiguity) and len(amb.candidates) == 2
    assert find_entity(TENANT, "Alex") is None  # a first name alone finds nobody
    assert find_entity(TENANT, "Dana Reyes", "organization") is None
    assert find_entity(OTHER, "Dana Reyes") is None

    assert await get_entity("dana@example.com", tenant_id=TENANT) is None
    entity = await get_entity("Dana Reyes", tenant_id=TENANT)
    assert entity and entity["id"] == node and "embedding" not in entity
    assert {"id", "name", "entity_type", "aliases", "mention_count", "relations"} <= set(entity)

    # Ambiguous: today's best-connected fallback still answers.
    busy = world.entity_of(a)
    org = world.entity("Globex", "organization")
    world.q(
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "tenant_id) VALUES (%s, %s, 'works at', %s)",
        (busy, org, TENANT),
    )
    entity = await get_entity("alex morgan", tenant_id=TENANT)
    assert entity and entity["id"] == busy and len(entity["relations"]) == 1
    # Nothing was recorded by any of these lookups.
    after = world.snapshot()
    assert after["memory_resolution_decisions"] == before["memory_resolution_decisions"]
    assert after["memory_mentions"] == before["memory_mentions"]


# ═══ adversarial fixture: precision and recall ═══════════════════════════


def test_adversarial_identity_set_precision_and_recall(world: World) -> None:
    from robothor.memory.identity import resolve_mentions, sync_crm_entities, write_mentions

    nw = world.company("Northwind Traders", "northwind.example")
    gx = world.company("Globex", "globex.example")
    it = world.company("Initech", "initech.example")
    jl_org = world.company("Jordan Lee")
    p = {
        "dana": world.person("Dana", "Reyes", "dana@example.com"),
        "bob_o": world.person("Bob", "Okafor", "bob.okafor@example.com"),
        "am_n": world.person("Alex", "Morgan", "alex@northwind.example", company=nw),
        "am_g": world.person("Alex", "Morgan", "amorgan@globex.example", company=gx),
        "chen": world.person("Alex", "Chen", "alex.chen@globex.example", company=gx),
        "rr": world.person("Robert", "Rivera", "robert@initech.example", company=it),
        "sl": world.person("Samantha", "Lee", "sam.lee@example.com"),
        "sc": world.person("Samantha", "Cole", "sam.cole@example.com"),
        "jl": world.person("Jordan", "Lee", "jordan@example.com"),
        "op": world.person("Casey", "Quinn", "casey@example.com"),
    }
    world.owner(p["op"])
    world.channel(p["rr"], "telegram", "4242")
    other_chen = world.person("Alex", "Chen", tenant=OTHER)
    sync_crm_entities(TENANT)
    sync_crm_entities(OTHER)
    n = {k: world.entity_of(v) for k, v in p.items()}
    n["globex"] = world.entity_of(gx)
    n["jl_org"] = world.entity_of(jl_org)

    fid = world.fact("Alex Chen and Casey Quinn met", ["Alex Chen", "Casey Quinn"], session="c1")
    seed = [MentionInput("Alex Chen", "person"), MentionInput("Casey Quinn", "person")]
    write_mentions(TENANT, fid, seed, resolve_mentions(TENANT, seed))

    mi = MentionInput
    cases: list[tuple[MentionInput, int | None]] = [
        (mi("Dana Reyes", "person", strong_keys={"email": "dana@example.com"}), n["dana"]),
        (mi("Dana Reyes", "person"), n["dana"]),
        (mi("  dana   REYES ", "person"), n["dana"]),
        (mi("dana reyes", None), n["dana"]),
        (mi("Dana Reyes", "person", strong_keys={"email": "bob.okafor@example.com"}), n["bob_o"]),
        (mi("Alex", "person"), None),
        (mi("Alex", None), None),
        (mi("Alex Morgan", "person"), None),
        (mi("Alex Morgan", "person", context={"organisation": "Northwind Traders"}), n["am_n"]),
        (mi("Alex Morgan", "person", context={"email_domain": "globex.example"}), n["am_g"]),
        (mi("Alex Morgan", "person", context={"email_domain": "gmail.com"}), None),
        (mi("Alex Morgan", "person", context={"co_mentions": ["Initech"]}), None),
        (mi("Alex", "person", context={"conversation_ref": "c1"}), n["chen"]),
        (mi("Alex", "person", context={"conversation_ref": "c2"}), None),
        (mi("Casey", "person", context={"conversation_ref": "c1"}), None),
        (mi("Casey Quinn", "person"), n["op"]),
        (mi("Sam", "person"), None),
        (mi("Samantha", "person"), None),
        (mi("Bob", "person"), None),
        (mi("Bob Rivera", "person"), None),
        (mi("Bob Rivera", "person", context={"organisation": "Initech"}), n["rr"]),
        (mi("Robert Rivera", "person"), n["rr"]),
        (mi("Ro", "person", strong_keys={"channel": "telegram:4242"}), n["rr"]),
        (mi("Jordan Lee", "person"), n["jl"]),
        (mi("Jordan Lee", "organization"), n["jl_org"]),
        (mi("Jordan Lee", None), None),
        (mi("Jordan", None), None),
        (mi("Globex", "organization"), n["globex"]),
        (mi("Alex Chen", "person"), n["chen"]),
        (mi("Lee", "person"), None),
        (mi("A. Morgan", "person"), None),
        (mi("Dana", "person", strong_keys={"crm_person_id": other_chen}), None),
    ]
    results = resolve_mentions(TENANT, [c for c, _ in cases])
    resolved = correct = expected_resolvable = 0
    wrong: list[str] = []
    for (case, expected), got in zip(cases, results, strict=True):
        if expected is not None:
            expected_resolvable += 1
        if got.entity_id is not None:
            resolved += 1
            if got.entity_id == expected:
                correct += 1
            else:
                wrong.append(f"{case.surface_form!r} -> {got.entity_id} ({got.method})")
        elif expected is not None:
            wrong.append(f"{case.surface_form!r} unresolved, expected {expected}")
    precision = correct / resolved if resolved else 1.0
    recall = correct / expected_resolvable if expected_resolvable else 1.0
    print(
        f"\nadversarial identity set: {len(cases)} mentions, {expected_resolvable} resolvable; "
        f"resolved {resolved}, correct {correct}; precision {precision:.3f}, recall {recall:.3f}"
    )
    assert not wrong, wrong
    assert precision == 1.0 and recall == 1.0


# ═══ review fixes ════════════════════════════════════════════════════════


async def test_get_entity_exposes_no_crm_contact_data(world: World) -> None:
    """A reader without CRM access must not learn emails or handles through memory."""
    from robothor.memory.entities import get_entity
    from robothor.memory.identity import find_entity, sync_crm_entities

    org = world.company("Northwind Traders", "northwind.example")
    pid = world.person(
        "Dana", "Reyes", "dana@example.com", extra_emails=("dr@example.org",), company=org
    )
    world.q(
        "INSERT INTO contact_identifiers (channel, identifier, display_name, person_id, tenant_id) "
        "VALUES ('telegram', '777', 'dana_r_handle', %s, %s)",
        (pid, TENANT),
    )
    sync_crm_entities(TENANT)
    stored = json.dumps(
        world.q(
            "SELECT name, aliases, metadata FROM memory_entities WHERE tenant_id = %s", (TENANT,)
        ),
        default=str,
    )
    for secret in ("dana@example.com", "dr@example.org", "dana_r_handle", "northwind.example"):
        assert secret not in stored

    entity = await get_entity("Dana Reyes", tenant_id=TENANT)
    assert entity is not None
    shown = json.dumps(entity, default=str)
    assert "@" not in shown and "dana_r_handle" not in shown
    for probe in ("dana@example.com", "dr@example.org", "northwind.example"):
        assert await get_entity(probe, tenant_id=TENANT) is None
        assert find_entity(TENANT, probe) is None
    # Resolution on the write side still uses the CRM's emails as strong keys.
    out = _one(MentionInput("D", "person", strong_keys={"email": "dr@example.org"}))
    assert out.entity_id == world.entity_of(pid)


async def test_a_single_token_crm_person_never_takes_over_a_first_name_node(world: World) -> None:
    from robothor.memory.entities import upsert_entity
    from robothor.memory.identity import find_entity, sync_crm_entities

    legacy = world.entity("Alex", "person")  # built from every extracted bare "Alex"
    alex = world.person("Alex", "", "alex.solo@example.com")
    sam = world.person("Sam", "", "sam.solo@example.com")
    world.person("A.", "Morgan", "a.morgan@example.com")
    report = sync_crm_entities(TENANT)
    assert report["people"]["linked"] == 0

    assert world.one("SELECT crm_person_id FROM memory_entities WHERE id = %s", (legacy,)) is None
    node = world.entity_of(alex)
    assert node != legacy
    names = {
        r[0] for r in world.q("SELECT name FROM memory_entities WHERE crm_person_id IS NOT NULL")
    }
    assert not names & {"Alex", "Sam", "A. Morgan"}
    assert world.count("memory_resolution_decisions", TENANT) == 0  # no link was recorded

    # The legacy extraction path keeps writing bare names to the unlinked node,
    # never to a CRM person's node.
    assert await upsert_entity("Alex", "person", tenant_id=TENANT) == legacy
    assert await upsert_entity("Sam", "person", tenant_id=TENANT) != world.entity_of(sam)
    found = find_entity(TENANT, "Alex")
    assert isinstance(found, dict) and found["id"] == legacy
    # A strong key still reaches the CRM person.
    out = _one(MentionInput("Alex", "person", strong_keys={"crm_person_id": alex}))
    assert out.entity_id == node
    assert sync_crm_entities(TENANT)["people"]["unchanged"] == 3


def test_a_first_name_shared_by_the_operator_and_a_contact_stays_unresolved(world: World) -> None:
    from robothor.memory.identity import resolve_mentions, sync_crm_entities, write_mentions

    op = world.person("Casey", "Quinn", "casey@example.com")
    world.owner(op)
    world.person("Casey", "Lin", "casey.lin@example.com")
    sync_crm_entities(TENANT)
    fid = world.fact("Casey Quinn and Casey Lin met", ["Casey Quinn", "Casey Lin"], session="s1")
    seed = [MentionInput("Casey Quinn", "person"), MentionInput("Casey Lin", "person")]
    write_mentions(TENANT, fid, seed, resolve_mentions(TENANT, seed))
    out = _one(MentionInput("Casey", "person", context={"conversation_ref": "s1"}))
    assert out.entity_id is None


def test_reversing_a_crm_merge_returns_post_merge_mentions_to_the_loser(world: World) -> None:
    from robothor.crm import dal
    from robothor.memory.identity import (
        resolve_mentions,
        reverse_decision,
        sync_crm_entities,
        write_mentions,
    )

    keeper = world.person("Dana", "Reyes", "dana@example.com")
    loser = world.person("Danielle", "Reyes", "danielle@example.org")
    sync_crm_entities(TENANT)
    keeper_node, loser_node = world.entity_of(keeper), world.entity_of(loser)
    before = world.fact("Danielle Reyes called", ["Danielle Reyes"])
    m = MentionInput("Danielle Reyes", "person")
    write_mentions(TENANT, before, [m], resolve_mentions(TENANT, [m]))
    assert dal.merge_people(keeper, loser, TENANT)

    after = world.fact("Danielle Reyes signed the lease", ["Danielle Reyes"])
    write_mentions(TENANT, after, [m], resolve_mentions(TENANT, [m]))
    keyed = world.fact("She paid", ["Dani"])
    k = MentionInput("Dani", "person", strong_keys={"crm_person_id": loser})
    write_mentions(TENANT, keyed, [k], resolve_mentions(TENANT, [k]))
    unrelated = world.fact("Dana Reyes likes tea", ["Dana Reyes"])
    d = MentionInput("Dana Reyes", "person")
    write_mentions(TENANT, unrelated, [d], resolve_mentions(TENANT, [d]))
    assert world.one("SELECT entity_id FROM memory_mentions WHERE fact_id = %s", (after,)) == (
        keeper_node
    )
    org = world.entity("Northbank", "organization")
    rel = world.one(
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "tenant_id) VALUES (%s, %s, 'banks with', %s) RETURNING id",
        (keeper_node, org, TENANT),
    )
    world.q(
        "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) VALUES (%s, %s, %s)",
        (TENANT, rel, after),
    )

    merge = world.one(
        "SELECT id FROM memory_resolution_decisions WHERE evidence->>'basis' = %s",
        (f"crm_merge:person:{loser}",),
    )
    report = reverse_decision(TENANT, merge, reason="different people")
    assert set(report["fact_ids"]) == {after, keyed}
    assert report["relation_ids"] == [rel]
    rows = dict(world.q("SELECT fact_id, entity_id FROM memory_mentions"))
    assert rows[after] == loser_node and rows[keyed] == loser_node
    assert rows[before] == loser_node and rows[unrelated] == keeper_node


def test_a_reversal_can_be_previewed_without_writing(world: World) -> None:
    from robothor.memory.identity import reverse_decision

    w = _wrongly_resolved(world)
    tables = ("memory_resolution_decisions", "memory_mentions")
    before = {t: world.q(f"SELECT * FROM {t} ORDER BY 1") for t in tables}  # noqa: S608
    preview = reverse_decision(
        TENANT, w["decision"], reason="look", new_entity_id=w["right"], dry_run=True
    )
    assert preview["dry_run"] is True and sorted(preview["fact_ids"]) == sorted(w["facts"])
    assert len(preview["mention_ids"]) == 2
    for t, rows in before.items():
        assert world.q(f"SELECT * FROM {t} ORDER BY 1") == rows  # noqa: S608


def test_correcting_one_fact_moves_only_that_mention(world: World) -> None:
    from robothor.memory.identity import reverse_decision

    w = _wrongly_resolved(world)
    fixed, untouched = w["facts"]
    report = reverse_decision(
        TENANT, w["decision"], reason="namesake here", new_entity_id=w["right"], fact_ids=[fixed]
    )
    assert report["fact_ids"] == [fixed] and report["relation_ids"] == [w["rel"]]
    rows = dict(
        world.q(
            "SELECT fact_id, entity_id FROM memory_mentions WHERE surface_form = 'Robert Rivera'"
        )
    )
    assert rows == {fixed: w["right"], untouched: w["wrong"]}
    # The shared decision stays live, and new mentions are not steered by the correction.
    assert (
        world.one(
            "SELECT reversed_at FROM memory_resolution_decisions WHERE id = %s", (w["decision"],)
        )
        is None
    )
    again = _one(w["mention"])
    assert (again.entity_id, again.decision_id) == (w["wrong"], w["decision"])
    with pytest.raises(LookupError):
        reverse_decision(TENANT, w["decision"], reason="x", fact_ids=[987654])


def test_settling_one_bare_first_name_leaves_the_others_unresolved(world: World) -> None:
    from robothor.memory.identity import backfill_mentions, reverse_decision, sync_crm_entities

    morgan = world.person("Alex", "Morgan")
    world.person("Alex", "Chen")
    sync_crm_entities(TENANT)
    one = world.fact("Alex called", ["Alex"])
    two = world.fact("Alex wrote", ["Alex"])
    backfill_mentions(TENANT, dry_run=False)
    shared = world.q("SELECT DISTINCT decision_id FROM memory_mentions")
    assert len(shared) == 1
    decision = shared[0][0]
    node = world.entity_of(morgan)
    with pytest.raises(ValueError):  # settling an unresolved name in bulk is refused
        reverse_decision(TENANT, decision, reason="bulk", new_entity_id=node)
    reverse_decision(TENANT, decision, reason="this one is Morgan", new_entity_id=node,
                     fact_ids=[one])  # fmt: skip
    rows = dict(world.q("SELECT fact_id, entity_id FROM memory_mentions"))
    assert rows == {one: node, two: None}
    assert _one(MentionInput("Alex", None)).entity_id is None
    assert _one(MentionInput("Alex", "person")).entity_id is None


def test_undoing_a_mistaken_reversal_restores_the_name(world: World) -> None:
    from robothor.memory.identity import reverse_decision

    w = _wrongly_resolved(world)
    first = reverse_decision(TENANT, w["decision"], reason="looked wrong", new_entity_id=w["right"])
    reverse_decision(
        TENANT, first["new_decision_id"], reason="it was right after all", new_entity_id=w["wrong"]
    )
    assert _one(w["mention"]).entity_id == w["wrong"]


def test_a_rejection_recorded_untyped_also_binds_typed_mentions(world: World) -> None:
    from robothor.memory.identity import resolve_mentions, reverse_decision, write_mentions

    wrong = world.entity("Robert Rivera", "person")
    fid = world.fact("Robert Rivera wrote", ["Robert Rivera"])
    m = MentionInput("Robert Rivera", None)  # as the backfill writes it
    out = resolve_mentions(TENANT, [m])
    assert out[0].entity_id == wrong
    write_mentions(TENANT, fid, [m], out)
    reverse_decision(TENANT, out[0].decision_id, reason="not this one")
    assert _one(MentionInput("Robert Rivera", "person")).entity_id is None
    assert _one(MentionInput("Robert Rivera", None)).entity_id is None


def test_the_nightly_sync_repairs_a_merge_whose_hook_failed(world: World, monkeypatch) -> None:
    from robothor.crm import dal, hooks
    from robothor.memory.identity import reverse_decision, sync_crm_entities

    keeper = world.person("Dana", "Reyes", "dana@example.com")
    loser = world.person("Danielle", "Reyes", "danielle@example.org")
    sync_crm_entities(TENANT)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(hooks, "_crm_merge_recorder", _boom)
    assert dal.merge_people(keeper, loser, TENANT)
    basis = f"crm_merge:person:{loser}"
    count = "SELECT count(*) FROM memory_resolution_decisions WHERE evidence->>'basis' = %s"
    assert world.one(count, (basis,)) == 0

    report = sync_crm_entities(TENANT)
    assert report["merges_recorded"] == 1
    assert world.one(count, (basis,)) == 1
    keeper_node = world.entity_of(keeper)
    out = _one(MentionInput("x", "person", strong_keys={"crm_person_id": loser}))
    assert out.entity_id == keeper_node
    # An operator's reversal of that merge is not undone by the next sync.
    merge = world.one(
        "SELECT id FROM memory_resolution_decisions WHERE evidence->>'basis' = %s", (basis,)
    )
    reverse_decision(TENANT, merge, reason="different people")
    assert sync_crm_entities(TENANT)["merges_recorded"] == 0
    assert world.one(count, (basis,)) == 1
