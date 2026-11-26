"""The graph leg of a search ranks what the walk reaches by the question.

On real Postgres, through ``activation.graph_candidates_for_query``. The walk
reaches the right entity; what it returns must be the facts about that entity
that answer what was asked, not the entity's most important facts:

* among a reached entity's facts, the one closest in meaning to the question
  (its embedding against the embedding of the question with the names it
  links removed) comes first, whatever the importance of the others, and
  whether or not it shares a word with the question;
* a fact about an entity the question itself names is left to the dense and
  keyword legs, which find it by name; the graph returns what they cannot;
* a reached fact below the similarity floor is not returned, and a question
  with nothing beyond the names it links ("Alice Smith", "Who is Alice
  Smith?") gets no graph rows at all, nor does one without an embedding;
* an inactive fact never surfaces;
* every relevance-ranked fact still obeys tenant, reader, person scope and
  trust: a private or foreign fact that matches the question word for word
  never surfaces.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from robothor.memory.tests.fake_embedding import embed_all_facts, fake_embed
from robothor.memory.tests.test_graph_activation import OTHER, TENANT, Graph

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"
_SUFFIX = "_memory_graph_question_relevance.sql"

QUESTION = "In which city is Alice Smith's employer headquartered?"


def _migration() -> str:
    found = [
        line.strip() for line in MANIFEST.read_text().splitlines() if line.strip().endswith(_SUFFIX)
    ]
    return Path(found[0]).stem if len(found) == 1 else f"missing{_SUFFIX}"


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
    # No operator is configured in the scratch database.
    from robothor.memory import activation

    monkeypatch.setattr(activation, "operator_person_id", lambda *_a, **_k: None)
    graph = Graph(db)
    graph.connect = _conn  # type: ignore[attr-defined]
    _CURRENT[:] = [graph]
    yield graph
    _CURRENT.clear()


#: The world of the running test, so ``_search`` can embed what it planted.
_CURRENT: list[Graph] = []


def _employer_world(world: Graph, *, tenant: str = TENANT) -> dict[str, int]:
    alice = world.entity("Alice Smith", tenant=tenant)
    org = world.entity("Quorvane Example", entity_type="organization", tenant=tenant)
    world.link(alice, org, tenant=tenant)
    # The organisation's most important facts say nothing about where it is.
    noise = [
        world.fact(f"Quorvane Example won industry award {i}", about=(org,), importance=0.95,
                   tenant=tenant)
        for i in range(6)
    ]  # fmt: skip
    hq = world.fact(
        "Quorvane Example is headquartered in Lisbon", about=(org,), importance=0.1, tenant=tenant
    )
    return {"alice": alice, "org": org, "hq": hq, "noise": set(noise)}


def _also_mentions(world: Graph, fact_id: int, entity_id: int) -> None:
    """A second mention (``Graph.mention`` writes one surface per fact)."""
    world.sql(
        "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form) "
        "VALUES (%s, %s, %s, %s)",
        (TENANT, fact_id, entity_id, f"surface {entity_id}"),
    )


def _search(query: str = QUESTION, **kw: Any) -> list[Any]:
    """The search leg, with every planted fact embedded by the fake model."""
    from robothor.memory.activation import graph_candidates_for_query

    embed_all_facts(_CURRENT[0].sql)
    kw.setdefault("embed", fake_embed)
    kw.setdefault("tenant_id", TENANT)
    tenant = kw.pop("tenant_id")
    return graph_candidates_for_query(tenant, query, **kw)


def _ids(cands: list[Any]) -> list[int]:
    return [int(c.id) for c in cands]


def test_the_reached_fact_that_answers_the_question_comes_first(world: Graph) -> None:
    w = _employer_world(world)

    got = _search()

    assert got, "the walk from Alice Smith reached nothing"
    assert _ids(got)[0] == w["hq"], [c.text for c in got]
    first = got[0]
    assert first.payload["graph_path"] == ["Alice Smith", "Quorvane Example"]
    assert first.payload["graph_hops"] == 1
    assert first.payload["graph_relevance"] > 0
    assert first.leg_scores["graph"] > 0


def test_a_reached_fact_unrelated_to_the_question_is_left_out(world: Graph) -> None:
    """Facts the walk reaches but the question does not ask about crowd the
    results (raw detail fell from 43 to 31 of 110 on the eval world with them
    in): only reached facts at or above the similarity floor come back."""
    from robothor.memory.activation import GRAPH_MIN_SIMILARITY

    w = _employer_world(world)

    got = _search()

    assert w["hq"] in _ids(got)
    assert not set(_ids(got)) & w["noise"]
    assert all(c.payload["graph_relevance"] >= GRAPH_MIN_SIMILARITY for c in got)


def test_a_paraphrase_finds_the_fact_without_sharing_a_word(world: Graph) -> None:
    """ "based" is not in "headquartered": the graph ranks by meaning, so a
    reworded question still reaches the employer's address (on the eval
    world, full-text matching answered none of 30 reworded questions)."""
    w = _employer_world(world)

    got = _search("Where is Alice Smith's employer based?")

    assert _ids(got)[:1] == [w["hq"]], [c.text for c in got]


@pytest.mark.parametrize("query", ["Alice Smith", "Who is Alice Smith?", "alice smith's"])
def test_a_question_with_nothing_beyond_a_name_gets_no_graph_rows(world: Graph, query: str) -> None:
    """Once the name is removed nothing is asked, so nothing reached is
    relevant: a bare name used to give its two reserved places to unranked
    neighbours (an external-origin fact two hops away among them)."""
    _employer_world(world)
    assert _search("In which city is Alice Smith's employer headquartered?"), "negative control"

    assert _search(query) == []


def test_without_the_questions_embedding_the_graph_returns_nothing(world: Graph) -> None:
    _employer_world(world)

    assert _search(embed=lambda _text: None) == []


def test_a_relation_path_never_names_an_entity_the_reader_cannot_see(world: Graph) -> None:
    """The path a graph row is labelled with is the walk's own first-reach
    path. A node private to Bob, on the strongest route, is on Bob's path and
    never on anyone else's (nor in the text that names it)."""
    from robothor.memory.recall import row_text

    alice_reader, bob_reader = str(uuid.uuid4()), str(uuid.uuid4())
    w = _employer_world(world)
    hidden = world.entity("Pat Hidden", scope_id=bob_reader)
    world.link(w["alice"], hidden, weight=5.0)
    world.link(hidden, w["org"], weight=5.0)

    def labels(reader: str) -> list[str]:
        got = _search(reader_scope_id=reader)
        assert any(int(c.id) == w["hq"] for c in got), "negative control"
        return [
            row_text({**c.payload, "leg_ranks": {"graph": 0}})
            + " "
            + " ".join(c.payload["graph_path"])
            for c in got
        ]

    assert not any("Pat Hidden" in text for text in labels(alice_reader))
    assert not any("Pat Hidden" in text for text in labels(""))
    assert any("Pat Hidden" in text for text in labels(bob_reader)), "negative control"


def test_an_inactive_fact_never_surfaces(world: Graph) -> None:
    w = _employer_world(world)
    old = world.fact(
        "Quorvane Example was headquartered in Madrid city", about=(w["org"],), active=False
    )
    assert w["hq"] in _ids(_search()), "negative control"
    assert old not in _ids(_search())


def test_importance_does_not_outrank_the_question(world: Graph) -> None:
    """Six facts of importance 0.95 against one of 0.1: the relevant one is
    still returned within the per-entity cap."""
    from robothor.memory.activation import FACTS_PER_ENTITY

    w = _employer_world(world)
    assert len(w["noise"]) > FACTS_PER_ENTITY

    assert w["hq"] in _ids(_search())


def test_facts_about_the_entity_the_question_names_are_left_to_the_other_legs(
    world: Graph,
) -> None:
    w = _employer_world(world)
    joined = world.fact("Alice Smith joined Quorvane Example as an engineer", about=(w["alice"],))
    _also_mentions(world, joined, w["org"])
    lives = world.fact("Alice Smith lives in Porto", about=(w["alice"],))

    got = set(_ids(_search()))

    assert w["hq"] in got
    assert joined not in got and lives not in got


def test_a_seed_only_mentioned_by_a_top_hit_keeps_its_facts(world: Graph) -> None:
    """The question names no one: facts about the entities its top hits
    mention are the graph's to return."""
    w = _employer_world(world)
    hit = world.fact("The Aurora project is run by Quorvane Example", about=(w["org"],))

    got = _ids(_search("Where is the organisation headquartered?", seed_fact_ids=(hit,)))

    assert got and got[0] == w["hq"]


# ─── isolation: relevance never widens what the walk may return ──────────


def test_a_private_fact_matching_the_question_is_not_returned_to_another_reader(
    world: Graph,
) -> None:
    alice_reader, bob_reader = str(uuid.uuid4()), str(uuid.uuid4())
    w = _employer_world(world)
    private = world.fact(
        "Quorvane Example is secretly headquartered in Geneva city",
        about=(w["org"],),
        scope_id=bob_reader,
    )

    alice = set(_ids(_search(reader_scope_id=alice_reader)))
    anonymous = set(_ids(_search()))
    bob = set(_ids(_search(reader_scope_id=bob_reader)))

    assert w["hq"] in alice and private not in alice
    assert private not in anonymous
    assert private in bob, "negative control: its owner's search returns it"


def test_another_persons_fact_matching_the_question_stays_out_of_a_restricted_scope(
    world: Graph,
) -> None:
    alice_pid, bob_pid = world.person("Alice", "Smith"), world.person("Bob", "Jones")
    w = _employer_world(world)
    bobs = world.fact(
        "Quorvane Example headquartered city for Bob is Madrid",
        about=(w["org"],),
        person_id=bob_pid,
    )

    assert bobs in _ids(_search()), "negative control: unrestricted, it is returned"
    scoped = set(_ids(_search(person_restricted=True, person_id=alice_pid)))
    assert bobs not in scoped and w["hq"] in scoped


def test_another_tenants_matching_facts_never_surface(world: Graph) -> None:
    mine = _employer_world(world)
    theirs = _employer_world(world, tenant=OTHER)
    # A mention row of mine pointing at the other tenant's matching fact.
    leaked = world.fact("Quorvane Example is headquartered in Oslo city", tenant=OTHER)
    world.mention(leaked, mine["org"], tenant=TENANT)

    got = set(_ids(_search()))

    assert mine["hq"] in got
    assert theirs["hq"] not in got and leaked not in got
    assert not got & theirs["noise"]


_COUNT = (
    "SELECT count(*) FROM memory_activate_for_question(%s, ARRAY[%s]::int[], "
    "p_query => 'city employer headquartered', p_query_embedding => %s::vector, "
    "p_trusted_only => false)"
)


def test_a_session_scoped_to_another_tenant_gets_nothing(world: Graph) -> None:
    w = _employer_world(world)
    embed_all_facts(world.sql)
    args = (TENANT, w["alice"], str(fake_embed("In which city is employer headquartered?")))
    assert world.sql(_COUNT, args)[0][0] > 0, "negative control"
    world.sql("SELECT set_config('app.tenant_id', %s, false)", (OTHER,))
    try:
        assert world.sql(_COUNT, args)[0][0] == 0
    finally:
        world.sql("SELECT set_config('app.tenant_id', '', false)")


def test_the_function_refuses_a_foreign_session_itself(world: Graph) -> None:
    """Not only through the walk it calls: with a walk that ignores the
    session (planted in this scratch database), the function still returns
    nothing to a session scoped to another tenant."""
    w = _employer_world(world)
    embed_all_facts(world.sql)
    world.sql(
        "DROP FUNCTION memory_activate(text, integer[], double precision[], text, integer, "
        "integer, double precision, integer[], boolean, integer, integer, boolean, uuid, uuid)"
    )
    world.sql(
        "CREATE FUNCTION memory_activate(text, integer[], double precision[], text, "
        "integer, integer, double precision, integer[], boolean, integer, integer, boolean, uuid, "
        "uuid) RETURNS TABLE (kind text, entity_id integer, entity_name text, score double "
        "precision, hops integer, via_edge_id integer, path_names text[], fact_id integer, "
        "fact_text text, category text, origin text, importance_score double precision, "
        "created_at timestamptz, person_id uuid, entities text[], access_count integer) "
        "LANGUAGE sql AS $$ SELECT 'entity'::text, id, name::text, 1.0::float8, "
        "CASE WHEN id = $2[1] THEN 0 ELSE 1 END, NULL::int, NULL::text[], NULL::int, NULL::text, "
        "NULL::text, NULL::text, NULL::float8, NULL::timestamptz, NULL::uuid, NULL::text[], "
        "NULL::int FROM memory_entities WHERE tenant_id = $1 $$"
    )
    args = (TENANT, w["alice"], str(fake_embed("In which city is employer headquartered?")))
    assert world.sql(_COUNT, args)[0][0] > 0, "negative control: the stub walk answers"
    world.sql("SELECT set_config('app.tenant_id', %s, false)", (OTHER,))
    try:
        assert world.sql(_COUNT, args)[0][0] == 0
    finally:
        world.sql("SELECT set_config('app.tenant_id', '', false)")


def test_trusted_mode_never_returns_external_content_however_relevant(world: Graph) -> None:
    from robothor.memory.activation import activate_for_question

    w = _employer_world(world)
    planted = world.fact(
        "An anonymous post claims Quorvane Example is headquartered in Atlantis city",
        about=(w["org"],),
        origin="external",
    )
    from robothor.memory.activation import Seed, question_without_names

    embed_all_facts(world.sql)
    seeds = [Seed(entity_id=w["alice"], name="Alice Smith")]
    emb = fake_embed(question_without_names(QUESTION, seeds))

    def walk(trusted: bool) -> set[int]:
        return set(_ids(activate_for_question(
            TENANT, seeds, QUESTION, query_embedding=emb, trusted_only=trusted
        )))  # fmt: skip

    trusted, untrusted = walk(True), walk(False)

    assert w["hq"] in trusted and planted not in trusted
    assert planted in untrusted, "negative control: a deliberate search labels and returns it"
