"""Associative recall: bounded activation over the memory graph, on real Postgres.

Every test builds a scratch database from the canonical manifest (the graph
activation migration included) and plants entities, edges, edge evidence,
mentions and facts directly in SQL, so what is asserted is the database
function's own behaviour through ``robothor.memory.activation``:

* a fact two hops away is recalled at two hops and not at one;
* a hub linked to most of the graph does not flood the result, and an
  excluded entity is neither a seed nor a transit node;
* another tenant's entities, edges, evidence and mentions are never reached;
* the walk crosses only edges the reader can see, and an invisible edge
  changes no score; a private fact is not returned to another reader;
* in trusted mode only operator-confirmed, user-stated and CRM evidence counts
  (NULL origin does not), inferred and external edges are never followed;
* fan-out, hop cap, inactive edges and determinism.
"""

from __future__ import annotations

import itertools
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
_SUFFIX = "_memory_graph_activation.sql"

TENANT = "test-tenant"
OTHER = "test-tenant-b"


def _migration() -> str:
    """The manifest entry, found by name so a renumbering touches only the file."""
    found = [
        line.strip() for line in MANIFEST.read_text().splitlines() if line.strip().endswith(_SUFFIX)
    ]
    return Path(found[0]).stem if len(found) == 1 else f"missing{_SUFFIX}"


class Graph:
    """Plants graph rows straight into the scratch database."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._rel = itertools.count()
        for tenant in (TENANT, OTHER):
            self.sql(
                "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Test') "
                "ON CONFLICT DO NOTHING",
                (tenant,),
            )

    def sql(self, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.db.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else []

    def person(self, first: str, last: str, *, tenant: str = TENANT) -> str:
        """A CRM person (facts' person_id references one)."""
        pid = str(uuid.uuid4())
        self.sql(
            "INSERT INTO crm_people (id, first_name, last_name, tenant_id) VALUES (%s, %s, %s, %s)",
            (pid, first, last, tenant),
        )
        return pid

    def entity(
        self,
        name: str,
        *,
        tenant: str = TENANT,
        entity_type: str = "person",
        aliases: tuple[str, ...] = (),
        scope_id: str | None = None,
        crm_person_id: str | None = None,
    ) -> int:
        rows = self.sql(
            "INSERT INTO memory_entities (name, entity_type, tenant_id, aliases, scope_kind, "
            "scope_id, crm_person_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                name,
                entity_type,
                tenant,
                list(aliases),
                "user" if scope_id else "shared",
                scope_id,
                crm_person_id,
            ),
        )
        return int(rows[0][0])

    def fact(
        self,
        text: str,
        *,
        about: tuple[int, ...] = (),
        tenant: str = TENANT,
        origin: str | None = "user_stated",
        scope_id: str | None = None,
        active: bool = True,
        importance: float = 0.5,
        person_id: str | None = None,
        entities: tuple[str, ...] = (),
    ) -> int:
        rows = self.sql(
            "INSERT INTO memory_facts (fact_text, category, tenant_id, origin, scope_kind, "
            "scope_id, is_active, importance_score, person_id, entities) "
            "VALUES (%s, 'event', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                text,
                tenant,
                origin,
                "user" if scope_id else "shared",
                scope_id,
                active,
                importance,
                person_id,
                list(entities),
            ),
        )
        fid = int(rows[0][0])
        for entity_id in about:
            self.mention(fid, entity_id, tenant=tenant)
        return fid

    def mention(self, fact_id: int, entity_id: int, *, tenant: str = TENANT) -> None:
        self.sql(
            "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form) "
            "VALUES (%s, %s, %s, 'x')",
            (tenant, fact_id, entity_id),
        )

    def edge(
        self,
        a: int,
        b: int,
        *,
        evidence: tuple[int, ...] = (),
        legacy_fact: int | None = None,
        tenant: str = TENANT,
        evidence_tenant: str | None = None,
        weight: float = 1.0,
        origin: str | None = None,
        active: bool = True,
    ) -> int:
        rows = self.sql(
            "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
            "tenant_id, fact_id, weight, origin, is_active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (a, b, f"related_{next(self._rel)}", tenant, legacy_fact, weight, origin, active),
        )
        rid = int(rows[0][0])
        for fid in evidence:
            self.sql(
                "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
                "VALUES (%s, %s, %s)",
                (evidence_tenant or tenant, rid, fid),
            )
        return rid

    def link(self, a: int, b: int, *, origin: str | None = "user_stated", **kw: Any) -> int:
        """An edge with one shared supporting fact of ``origin``."""
        tenant = kw.get("tenant", TENANT)
        support = self.fact(f"link {a}-{b}", tenant=tenant, origin=origin)
        return self.edge(a, b, evidence=(support,), **kw)


@pytest.fixture
def world(scratch_db, monkeypatch) -> Iterator[Graph]:
    import psycopg2

    db, dsn = scratch_db(through=_migration())

    @contextmanager
    def _conn(*_a: Any, **_k: Any) -> Iterator[Any]:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    import robothor.db

    monkeypatch.setattr(robothor.db, "get_connection", _conn)
    graph = Graph(db)
    graph.connect = _conn  # type: ignore[attr-defined]
    yield graph


def _seed(entity_id: int, weight: float = 1.0) -> Any:
    from robothor.memory.activation import Seed

    return Seed(entity_id=entity_id, name=str(entity_id), weight=weight)


def _activate(tenant: str, seeds: list[int], **kw: Any) -> list[Any]:
    from robothor.memory.activation import activate

    kw.setdefault("trusted_only", True)
    return activate(tenant, [_seed(s) for s in seeds], **kw)


def _ids(cands: list[Any]) -> list[int]:
    return [int(c.id) for c in cands]


# ─── A–B–C: association across two hops ──────────────────────────────────


def test_a_fact_two_hops_away_is_recalled_at_two_hops_not_one(world: Graph) -> None:
    """Acceptance: A linked to B, B linked to C; asked about A, a fact about C
    is recalled."""
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    world.link(a, b)
    world.link(b, c)
    about_c = world.fact("Carol White runs the northern depot", about=(c,))

    two = _activate(TENANT, [a], max_hops=2)
    one = _activate(TENANT, [a], max_hops=1)

    assert about_c in _ids(two)
    assert about_c not in _ids(one)
    hit = next(x for x in two if x.id == about_c)
    assert hit.kind == "fact"
    assert hit.leg_scores["graph"] > 0
    assert hit.payload["graph_hops"] == 2
    assert hit.payload["graph_path"] == ["Alice Smith", "Bob Jones", "Carol White"]
    assert hit.payload["source"] == "graph"


def test_the_seed_and_its_neighbour_outrank_the_two_hop_fact(world: Graph) -> None:
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    world.link(a, b)
    world.link(b, c)
    about_b = world.fact("Bob Jones manages shipping", about=(b,))
    about_c = world.fact("Carol White runs the depot", about=(c,))

    scores = {int(x.id): x.leg_scores["graph"] for x in _activate(TENANT, [a])}

    assert scores[about_b] > scores[about_c] > 0


# ─── hub control ─────────────────────────────────────────────────────────


def _hub_world(world: Graph, spokes: int = 40) -> dict[str, Any]:
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    hub = world.entity("Hub Company", entity_type="organization")
    world.link(a, b)
    world.link(b, c)
    about_c = world.fact("Carol White runs the northern depot", about=(c,))
    chain_facts = {
        world.fact("Alice Smith keeps the passport", about=(a,)),
        world.fact("Alice Smith flies on Friday", about=(a,)),
        world.fact("Bob Jones manages shipping", about=(b,)),
        world.fact("Bob Jones booked the van", about=(b,)),
        about_c,
    }
    spoke_facts = []
    only_via_hub = []
    for i in range(spokes):
        s = world.entity(f"Spoke Person{i}")
        world.link(hub, s)
        spoke_facts.append(world.fact(f"Spoke Person{i} attended the offsite", about=(s,)))
    # The hub also touches the chain itself, as an operator node would.
    for x in (a, b, c):
        world.link(hub, x)
    hub_facts = [world.fact(f"Hub Company note {i}", about=(hub,)) for i in range(6)]
    # Reachable only through the hub.
    lonely = world.entity("Lonely Person")
    world.link(hub, lonely)
    only_via_hub.append(world.fact("Lonely Person likes tea", about=(lonely,)))
    return {
        "a": a,
        "hub": hub,
        "about_c": about_c,
        "chain_facts": chain_facts,
        "spoke_facts": set(spoke_facts),
        "hub_facts": set(hub_facts),
        "only_via_hub": set(only_via_hub),
    }


def test_a_hub_in_the_query_context_does_not_dominate_the_results(world: Graph) -> None:
    """Acceptance: a hub linked to everything appears in the query context;
    the results are not dominated by its unrelated facts."""
    from robothor.memory.activation import FACTS_PER_ENTITY

    w = _hub_world(world)
    got = _ids(_activate(TENANT, [w["a"], w["hub"]], limit=10))

    unrelated = w["spoke_facts"] | w["only_via_hub"]
    assert w["chain_facts"] <= set(got), "every fact on the Alice chain is recalled"
    assert len([f for f in got if f in w["hub_facts"]]) <= FACTS_PER_ENTITY
    last_chain = max(got.index(f) for f in w["chain_facts"])
    first_unrelated = min((got.index(f) for f in got if f in unrelated), default=len(got))
    assert last_chain < first_unrelated, "an unrelated fact of the hub outranked the chain"
    assert len([f for f in got if f in unrelated]) <= len(got) - len(w["chain_facts"])


def test_an_excluded_entity_is_neither_seed_nor_transit(world: Graph) -> None:
    w = _hub_world(world)

    got = set(_ids(_activate(TENANT, [w["a"], w["hub"]], exclude_entity_ids=(w["hub"],))))

    assert w["about_c"] in got, "the chain is still walked"
    assert not got & w["hub_facts"], "the excluded hub was used as a seed"
    assert not got & w["only_via_hub"], "the excluded hub was used as a transit node"
    assert not got & w["spoke_facts"]


def test_seed_weight_falls_with_visible_degree(world: Graph) -> None:
    """Inverse-degree seed weighting: a seed's own facts weigh less the more
    visible edges it has."""
    w = _hub_world(world)
    alice_fact = world.fact("Alice Smith pays the invoices", about=(w["a"],))
    scores = {
        int(x.id): x.leg_scores["graph"] for x in _activate(TENANT, [w["a"], w["hub"]], limit=50)
    }
    assert scores[alice_fact] > max(scores[f] for f in w["hub_facts"] if f in scores)


# ─── tenant isolation ────────────────────────────────────────────────────


def test_activation_never_reaches_another_tenant(world: Graph) -> None:
    """Acceptance: in tenant A, activation never reaches an entity of tenant B,
    even through rows planted with mismatched tenant ids."""
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    world.link(a, b)
    world.link(b, c)
    mine = world.fact("Carol White runs the depot", about=(c,))

    # Same names in the other tenant, fully linked there.
    oa, ob, oc = (world.entity(n, tenant=OTHER) for n in ("Alice Smith", "Bob Jones", "Dave Gray"))
    world.link(oa, ob, tenant=OTHER)
    world.link(ob, oc, tenant=OTHER)
    theirs = {
        world.fact("Dave Gray holds the other tenant's secret", about=(oc,), tenant=OTHER),
        world.fact("Bob Jones of the other tenant", about=(ob,), tenant=OTHER),
    }

    # Planted cross-tenant rows:
    # an edge row labelled with my tenant whose far end is the other tenant's entity,
    zed = world.entity("Zed Black", tenant=OTHER)
    world.link(a, zed)
    theirs.add(world.fact("Zed Black secret", about=(zed,), tenant=OTHER))
    # an edge row of the other tenant between two of my entities,
    yan = world.entity("Yan Green")
    theirs_edge_fact = world.fact("support", tenant=OTHER)
    world.edge(a, yan, evidence=(theirs_edge_fact,), tenant=OTHER)
    yan_fact = world.fact("Yan Green only reachable cross-tenant", about=(yan,))
    # an edge of mine whose only evidence is the other tenant's fact,
    xi = world.entity("Xi Brown")
    world.edge(a, xi, evidence=(theirs_edge_fact,))
    xi_fact = world.fact("Xi Brown only reachable through foreign evidence", about=(xi,))
    # and a mention row of mine pointing at the other tenant's fact.
    leaked = world.fact("other tenant fact mentioned from mine", tenant=OTHER)
    world.mention(leaked, b, tenant=TENANT)

    got = set(_ids(_activate(TENANT, [a], max_hops=3, limit=100)))

    assert mine in got
    assert not got & theirs
    assert yan_fact not in got and xi_fact not in got
    assert leaked not in got
    # Seeding my tenant's call with the other tenant's entity ids reaches nothing.
    assert _activate(TENANT, [oa, ob, oc]) == []


def test_a_session_scoped_to_another_tenant_gets_nothing(world: Graph) -> None:
    a, b = world.entity("Alice Smith"), world.entity("Bob Jones")
    world.link(a, b)
    world.fact("Bob Jones manages shipping", about=(b,))
    count = "SELECT count(*) FROM memory_activate(%s, ARRAY[%s]::int[], p_trusted_only => true)"
    assert world.sql(count, (TENANT, a))[0][0] > 0, "negative control: unscoped, it activates"
    world.sql("SELECT set_config('app.tenant_id', %s, false)", (OTHER,))
    try:
        assert world.sql(count, (TENANT, a))[0][0] == 0
    finally:
        world.sql("SELECT set_config('app.tenant_id', '', false)")


# ─── reader visibility ───────────────────────────────────────────────────


def _scores(cands: list[Any]) -> list[tuple[int, float]]:
    return [(int(c.id), round(c.leg_scores["graph"], 12)) for c in cands]


def test_an_edge_private_to_another_reader_is_not_walked_and_changes_no_score(
    world: Graph,
) -> None:
    alice_reader, bob_reader = str(uuid.uuid4()), str(uuid.uuid4())
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    world.link(a, b)
    world.link(a, c)
    world.fact("Bob Jones manages shipping", about=(b,))
    world.fact("Carol White runs the depot", about=(c,))
    before = _scores(_activate(TENANT, [a], reader_scope_id=alice_reader))
    assert before

    # Now an edge A–P whose only evidence is private to Bob.
    p = world.entity("Pat Hidden")
    secret = world.fact("Bob's private note", origin="user_stated", scope_id=bob_reader)
    world.edge(a, p, evidence=(secret,))
    about_p = world.fact("Pat Hidden lives on Elm Street", about=(p,))

    after = _scores(_activate(TENANT, [a], reader_scope_id=alice_reader))
    assert about_p not in [i for i, _ in after]
    assert after == before, "an edge the reader cannot see changed scores or order"

    # Bob can see his own evidence, so the walk crosses it for him.
    assert about_p in _ids(_activate(TENANT, [a], reader_scope_id=bob_reader))


def test_with_no_reader_only_shared_evidence_is_walked(world: Graph) -> None:
    owner = str(uuid.uuid4())
    a, p = world.entity("Alice Smith"), world.entity("Pat Hidden")
    secret = world.fact("private", scope_id=owner)
    world.edge(a, p, evidence=(secret,))
    about_p = world.fact("Pat Hidden detail", about=(p,))

    assert about_p not in _ids(_activate(TENANT, [a]))
    assert about_p in _ids(_activate(TENANT, [a], reader_scope_id=owner))


def test_a_private_fact_of_an_activated_entity_is_not_returned_to_another_reader(
    world: Graph,
) -> None:
    alice_reader, bob_reader = str(uuid.uuid4()), str(uuid.uuid4())
    a, b = world.entity("Alice Smith"), world.entity("Bob Jones")
    world.link(a, b)
    shared = world.fact("Bob Jones manages shipping", about=(b,))
    private = world.fact("Bob Jones's private diagnosis", about=(b,), scope_id=bob_reader)

    alice = set(_ids(_activate(TENANT, [a], reader_scope_id=alice_reader)))
    bob = set(_ids(_activate(TENANT, [a], reader_scope_id=bob_reader)))

    assert shared in alice and private not in alice
    assert private in bob


def test_a_restricted_person_scope_hides_another_persons_facts(world: Graph) -> None:
    """The existing "own data + shared" person scope also holds in the walk."""
    alice_pid, bob_pid = world.person("Alice", "Smith"), world.person("Bob", "Jones")
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    support = world.fact("support", person_id=bob_pid)
    world.edge(a, b, evidence=(support,))
    world.link(a, c)
    bobs = world.fact("Bob Jones fact owned by Bob", about=(b,), person_id=bob_pid)
    carols = world.fact("Carol White general fact", about=(c,))

    got = set(_ids(_activate(TENANT, [a], person_restricted=True, person_id=alice_pid, limit=50)))

    assert carols in got
    assert bobs not in got
    open_ = set(_ids(_activate(TENANT, [a], limit=50)))
    assert bobs in open_, "negative control: unrestricted, the fact is reachable"


# ─── trust ───────────────────────────────────────────────────────────────


def test_trusted_mode_follows_only_trusted_evidence_and_returns_only_trusted_facts(
    world: Graph,
) -> None:
    """Acceptance: an edge from external content does not contribute to
    automatic recall. NULL (legacy) origin counts as agent-generated, not
    trusted."""
    a = world.entity("Alice Smith")
    b, e, n = world.entity("Bob Jones"), world.entity("Eve External"), world.entity("Nora Legacy")
    world.link(a, b, origin="crm")
    world.link(a, e, origin="external")
    world.link(a, n, origin=None)
    bob_ok = world.fact("Bob Jones manages shipping", about=(b,), origin="operator_confirmed")
    bob_external = world.fact("Bob Jones is a fraud (web page)", about=(b,), origin="external")
    bob_legacy = world.fact("Bob Jones legacy note", about=(b,), origin=None)
    eve = world.fact("Eve External detail", about=(e,), origin="user_stated")
    nora = world.fact("Nora Legacy detail", about=(n,), origin="user_stated")

    trusted = {int(c.id): c for c in _activate(TENANT, [a], trusted_only=True)}
    assert bob_ok in trusted
    assert bob_external not in trusted and bob_legacy not in trusted
    assert eve not in trusted, "an edge evidenced only by external content was followed"
    assert nora not in trusted, "an edge evidenced only by NULL-origin content was followed"

    deliberate = {int(c.id): c for c in _activate(TENANT, [a], trusted_only=False)}
    assert {bob_ok, bob_external, bob_legacy, eve, nora} <= set(deliberate)
    assert deliberate[bob_external].payload["origin"] == "external"
    assert deliberate[bob_legacy].payload["origin"] == "agent_generated"
    assert deliberate[bob_legacy].payload["origin_recorded"] is None
    assert deliberate[bob_ok].payload["origin"] == "operator_confirmed"


def test_inferred_and_external_edges_are_never_eligible_in_trusted_mode(world: Graph) -> None:
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    world.link(a, b, origin="user_stated")
    # Trusted supporting facts, but the edge itself was inferred / external.
    world.edge(a, c, evidence=(world.fact("s1", origin="crm"),), origin="inferred")
    d = world.entity("Dan Ext")
    world.edge(a, d, evidence=(world.fact("s2", origin="crm"),), origin="external")
    about_c = world.fact("Carol White detail", about=(c,))
    about_d = world.fact("Dan Ext detail", about=(d,))

    trusted = set(_ids(_activate(TENANT, [a], trusted_only=True)))
    assert about_c not in trusted and about_d not in trusted
    assert {about_c, about_d} <= set(_ids(_activate(TENANT, [a], trusted_only=False)))


def test_legacy_null_origin_data_returns_nothing_in_trusted_mode(world: Graph) -> None:
    """Every row written before origins existed has NULL origin: automatic
    recall over it is empty by design, and deliberate recall still works."""
    a, b = world.entity("Alice Smith"), world.entity("Bob Jones")
    legacy = world.fact("Alice Smith knows Bob Jones", origin=None)
    world.edge(a, b, legacy_fact=legacy)  # the pre-evidence column only
    world.fact("Bob Jones manages shipping", about=(b,), origin=None)
    world.fact("Alice Smith keeps the passport", about=(a,), origin=None)

    assert _activate(TENANT, [a], trusted_only=True) == []
    assert len(_activate(TENANT, [a], trusted_only=False)) == 2


def test_the_legacy_fact_id_column_counts_as_evidence(world: Graph) -> None:
    a, b = world.entity("Alice Smith"), world.entity("Bob Jones")
    support = world.fact("Alice Smith knows Bob Jones", origin="user_stated")
    world.edge(a, b, legacy_fact=support)
    about_b = world.fact("Bob Jones manages shipping", about=(b,))
    assert about_b in _ids(_activate(TENANT, [a]))


# ─── bounds ──────────────────────────────────────────────────────────────


def test_fan_out_is_capped_to_the_strongest_edges(world: Graph) -> None:
    a = world.entity("Alice Smith")
    by_weight: dict[float, int] = {}
    for i in range(10):
        n = world.entity(f"Neighbour Person{i}")
        weight = 1.0 + i
        world.link(a, n, weight=weight)
        by_weight[weight] = world.fact(f"Neighbour Person{i} detail", about=(n,))

    got = set(_ids(_activate(TENANT, [a], fanout=3, limit=50)))

    neighbour_facts = set(by_weight.values())
    assert got & neighbour_facts == {by_weight[10.0], by_weight[9.0], by_weight[8.0]}


def test_hops_are_capped_at_three(world: Graph) -> None:
    names = ("Ann One", "Ben Two", "Cat Three", "Dan Four", "Eva Five")
    chain = [world.entity(n) for n in names]
    for x, y in itertools.pairwise(chain):
        world.link(x, y)
    facts = [world.fact(f"{n} detail", about=(e,)) for n, e in zip(names, chain, strict=True)]

    got = set(_ids(_activate(TENANT, [chain[0]], max_hops=10, limit=50)))

    assert facts[3] in got, "three hops are walked"
    assert facts[4] not in got, "max_hops=10 walked past the hard cap of three"
    assert got == set(_ids(_activate(TENANT, [chain[0]], max_hops=3, limit=50)))


def test_inactive_edges_are_ignored(world: Graph) -> None:
    a, b = world.entity("Alice Smith"), world.entity("Bob Jones")
    world.link(a, b, active=False)
    about_b = world.fact("Bob Jones manages shipping", about=(b,))
    assert about_b not in _ids(_activate(TENANT, [a], trusted_only=False))


def test_inactive_supporting_facts_do_not_carry_an_edge(world: Graph) -> None:
    a, b = world.entity("Alice Smith"), world.entity("Bob Jones")
    world.edge(a, b, evidence=(world.fact("retired support", active=False),))
    about_b = world.fact("Bob Jones manages shipping", about=(b,))
    assert about_b not in _ids(_activate(TENANT, [a], trusted_only=False))


def test_the_same_input_gives_the_same_order(world: Graph) -> None:
    w = _hub_world(world, spokes=12)
    runs = [
        [(c.id, c.leg_scores["graph"]) for c in _activate(TENANT, [w["a"], w["hub"]], limit=30)]
        for _ in range(3)
    ]
    assert runs[0] and runs[0] == runs[1] == runs[2]


def test_no_seeds_returns_nothing_without_a_query(world: Graph, monkeypatch) -> None:
    from robothor.memory import activation

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no seeds must not open a connection")

    monkeypatch.setattr("robothor.db.get_connection", _boom)
    assert activation.activate(TENANT, [], trusted_only=True) == []


# ─── seed linking ────────────────────────────────────────────────────────


def test_seeds_link_full_names_and_aliases_case_insensitively(world: Graph) -> None:
    from robothor.memory.activation import seeds_from_text

    alice = world.entity("Alice Smith", aliases=("Ally Smith",))
    acme = world.entity("Acme Logistics", entity_type="organization")
    world.entity("Alice Smith", tenant=OTHER)

    got = {s.entity_id for s in seeds_from_text(TENANT, "did alice smith call ACME LOGISTICS?")}
    assert got == {alice, acme}
    assert {s.entity_id for s in seeds_from_text(TENANT, "ask Ally Smith")} == {alice}


def test_a_single_token_name_is_never_a_seed(world: Graph) -> None:
    from robothor.memory.activation import seeds_from_text

    world.entity("Alice Smith")
    world.entity("Alice")  # even an entity whose whole name is one token
    assert seeds_from_text(TENANT, "Tell Alice about the plan") == []


def test_an_email_in_the_text_is_a_strong_key(world: Graph) -> None:
    from robothor.memory.activation import seeds_from_text

    alice = world.entity("Alice Smith")
    world.sql(
        "INSERT INTO contact_identifiers (channel, identifier, memory_entity_id, tenant_id) "
        "VALUES ('email', 'alice@example.com', %s, %s)",
        (alice, TENANT),
    )
    got = seeds_from_text(TENANT, "forward it to Alice@Example.com please")
    assert [s.entity_id for s in got] == [alice]
    assert got[0].method == "strong_key"


def test_extra_entity_ids_are_seeds_only_within_the_tenant(world: Graph) -> None:
    from robothor.memory.activation import seeds_from_text

    mine, theirs = world.entity("Alice Smith"), world.entity("Bob Jones", tenant=OTHER)
    got = seeds_from_text(TENANT, "hello there", extra_entity_ids=(mine, theirs))
    assert [s.entity_id for s in got] == [mine]


# ─── review hardening ────────────────────────────────────────────────────


def test_an_agent_scoped_row_never_counts_for_a_reader_with_the_same_id(world: Graph) -> None:
    """Visibility names the scope kind: a row private to an agent whose id
    happens to equal the reader's person id is not the reader's — not as
    edge evidence, not as a returned fact, not as an entity."""
    reader = str(uuid.uuid4())
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    agent_support = world.fact("agent's private support", scope_id=reader)
    world.edge(a, b, evidence=(agent_support,))
    world.link(a, c)
    agent_fact = world.fact("Carol White agent-private note", about=(c,), scope_id=reader)
    agent_entity = world.entity("Dan Agent", scope_id=reader)
    world.link(a, agent_entity)
    about_b = world.fact("Bob Jones manages shipping", about=(b,))
    about_d = world.fact("Dan Agent detail", about=(agent_entity,))

    # Negative control: as user-scoped rows the reader sees all of them.
    user_view = set(_ids(_activate(TENANT, [a], reader_scope_id=reader, limit=50)))
    assert {about_b, agent_fact, about_d} <= user_view

    world.sql(
        "UPDATE memory_facts SET scope_kind = 'agent' WHERE id = ANY(%s)",
        ([agent_support, agent_fact],),
    )
    world.sql("UPDATE memory_entities SET scope_kind = 'agent' WHERE id = %s", (agent_entity,))

    got = set(_ids(_activate(TENANT, [a], reader_scope_id=reader, limit=50)))
    assert about_b not in got, "an agent-private fact carried an edge for a person reader"
    assert agent_fact not in got, "an agent-private fact was returned to a person reader"
    assert about_d not in got, "an agent-private entity was walked for a person reader"


def test_the_operators_entities_are_excluded_by_crm_person_inside_the_walk(
    world: Graph,
) -> None:
    owner = world.person("Olive", "Operator")
    hub = world.entity("Olive Operator", crm_person_id=owner)
    a = world.entity("Alice Smith")
    lonely = world.entity("Lonely Person")
    world.link(a, hub)
    world.link(hub, lonely)
    hub_fact = world.fact("Olive Operator private calendar", about=(hub,))
    lonely_fact = world.fact("Lonely Person likes tea", about=(lonely,))
    alice_fact = world.fact("Alice Smith keeps the passport", about=(a,))

    assert {hub_fact, lonely_fact} <= set(_ids(_activate(TENANT, [a, hub], limit=50)))
    got = set(_ids(_activate(TENANT, [a, hub], exclude_crm_person_id=owner, limit=50)))

    assert alice_fact in got
    assert hub_fact not in got and lonely_fact not in got
