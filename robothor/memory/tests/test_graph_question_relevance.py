"""The graph leg of a search ranks what the walk reaches by the question.

On real Postgres, through ``activation.graph_candidates_for_query``. The walk
reaches the right entity; what it returns must be the facts about that entity
that answer what was asked, not the entity's most important facts:

* among a reached entity's facts, the one that shares the question's words
  (with the names the question links removed) comes first, whatever the
  importance of the others;
* a fact about an entity the question itself names is left to the dense and
  keyword legs, which find it by name; the graph returns what they cannot;
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
    yield graph


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
    from robothor.memory.activation import graph_candidates_for_query

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
    assert first.leg_scores["graph"] > max(
        c.leg_scores["graph"] for c in got if int(c.id) in w["noise"]
    )


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


def test_a_session_scoped_to_another_tenant_gets_nothing(world: Graph) -> None:
    w = _employer_world(world)
    count = (
        "SELECT count(*) FROM memory_activate_for_question(%s, ARRAY[%s]::int[], "
        "p_query => 'headquartered', p_trusted_only => false)"
    )
    assert world.sql(count, (TENANT, w["alice"]))[0][0] > 0, "negative control"
    world.sql("SELECT set_config('app.tenant_id', %s, false)", (OTHER,))
    try:
        assert world.sql(count, (TENANT, w["alice"]))[0][0] == 0
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
    from robothor.memory.activation import Seed

    seeds = [Seed(entity_id=w["alice"], name="Alice Smith")]
    trusted = set(_ids(activate_for_question(TENANT, seeds, QUESTION, trusted_only=True)))
    untrusted = set(_ids(activate_for_question(TENANT, seeds, QUESTION, trusted_only=False)))

    assert w["hq"] in trusted and planted not in trusted
    assert planted in untrusted, "negative control: a deliberate search labels and returns it"
