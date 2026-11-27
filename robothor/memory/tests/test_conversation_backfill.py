"""The backfill that makes old shared conversation facts as private as their chat.

Runs only when an operator runs it; dry run first. On the write-path
integration world's scratch database.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.memory.tests import test_write_path_integration as wp
from robothor.memory.tests.test_write_path_integration import (
    ALICE,
    ALICE_P,
    TENANT,
    World,
    store_chat,
)

pytestmark = pytest.mark.integration

database = wp.database
world = wp.world


def _old_fact(world: World, text: str, key: str, *, tenant: str = TENANT) -> int:
    """A fact as the previous conversation ingest wrote it: shared, with its session key."""
    return int(
        world.db.one(
            "INSERT INTO memory_facts (fact_text, category, tenant_id, source_type, metadata, "
            "content_hash) VALUES (%s, 'preference', %s, 'conversation', "
            "jsonb_build_object('session_key', %s::text), md5(%s)) RETURNING id",
            (text, tenant, key, text + key),
        )
    )


def _seed(world: World) -> dict[str, int]:
    store_chat(world.db, "telegram:4242", [("user", "hi")], person=ALICE)
    store_chat(world.db, "telegram:-100777", [("user", "hi all")])
    store_chat(world.db, "agent:main:room:3f2a0000-0000-4000-8000-000000000001", [("user", "x")])
    return {
        "mine": _old_fact(world, "Reader Person prefers dark roast coffee.", "telegram:4242"),
        "mine2": _old_fact(world, "Reader Person owns a red road bike.", "telegram:4242"),
        "group": _old_fact(world, "Someone in the group likes tea.", "telegram:-100777"),
        "room": _old_fact(
            world, "The room chose a venue.", "agent:main:room:3f2a0000-0000-4000-8000-000000000001"
        ),
        "gone": _old_fact(world, "A deleted chat said something.", "telegram:555"),
    }


def _scope(world: World, fact_id: int) -> tuple[Any, Any]:
    rows = world.db.q("SELECT scope_kind, scope_id FROM memory_facts WHERE id = %s", (fact_id,))
    return rows[0][0], rows[0][1]


def test_a_dry_run_reports_what_it_would_rescope_and_changes_nothing(world: World) -> None:
    from robothor.memory.conversation_backfill import rescope_conversation_facts

    ids = _seed(world)
    report = rescope_conversation_facts(TENANT)
    assert report["dry_run"] is True
    assert report["would_rescope"] == 2
    assert report["by_owner"] == {ALICE_P: 2}
    assert report["left_shared"] == {"no_owner": 1, "room": 1, "no_session": 1}
    assert all(_scope(world, i) == ("shared", None) for i in ids.values())


def test_applying_rescopes_one_to_one_facts_and_leaves_the_rest(world: World) -> None:
    from robothor.memory.conversation_backfill import rescope_conversation_facts

    ids = _seed(world)
    report = rescope_conversation_facts(TENANT, apply=True)
    assert report["rescoped"] == 2
    assert _scope(world, ids["mine"]) == ("user", ALICE_P)
    assert _scope(world, ids["mine2"]) == ("user", ALICE_P)
    for name in ("group", "room", "gone"):
        assert _scope(world, ids[name]) == ("shared", None)
    # The hash is the scoped one, so the fact can be edited like any private fact.
    from robothor.memory.drift import compute_fact_hash

    (text, digest) = world.db.q(
        "SELECT fact_text, content_hash FROM memory_facts WHERE id = %s", (ids["mine"],)
    )[0]
    assert digest == compute_fact_hash(
        text, tenant_id=TENANT, category="preference", scope_kind="user", scope_id=ALICE_P
    )


def test_it_resumes_in_batches_and_a_second_run_finds_nothing(world: World) -> None:
    from robothor.memory.conversation_backfill import rescope_conversation_facts

    _seed(world)
    first = rescope_conversation_facts(TENANT, apply=True, batch=1)
    assert first["rescoped"] == 1 and first["more"] is True
    second = rescope_conversation_facts(TENANT, apply=True, batch=1)
    assert second["rescoped"] == 1
    third = rescope_conversation_facts(TENANT, apply=True)
    assert third["rescoped"] == 0


def test_another_tenants_facts_are_never_touched(world: World) -> None:
    from robothor.memory.conversation_backfill import rescope_conversation_facts

    _seed(world)
    other = _old_fact(world, "Another tenant's fact.", "telegram:4242", tenant=wp.OTHER)
    rescope_conversation_facts(TENANT, apply=True)
    assert _scope(world, other) == ("shared", None)


def test_the_job_is_not_run_by_anything_automatically() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    callers = [
        p
        for p in (root / "robothor").rglob("*.py")
        if "tests" not in p.parts
        and p.name != "conversation_backfill.py"
        and "rescope_conversation_facts" in p.read_text()
    ]
    assert callers == []


# ═══ duplicates within a batch, and the graph left behind ════════════════


def _hashed_old_fact(world: World, text: str, key: str, person: str | None) -> int:
    from robothor.memory.drift import compute_fact_hash

    digest = compute_fact_hash(text, tenant_id=TENANT, category="personal", person_id=person)
    return int(
        world.db.one(
            "INSERT INTO memory_facts (fact_text, category, tenant_id, source_type, metadata, "
            "person_id, content_hash) VALUES (%s, 'personal', %s, 'conversation', "
            "jsonb_build_object('session_key', %s::text), %s, %s) RETURNING id",
            (text, TENANT, key, person, digest),
        )
    )


def test_two_shared_copies_that_meet_in_one_scope_keep_the_oldest(world: World) -> None:
    from robothor.memory.conversation_backfill import rescope_conversation_facts

    store_chat(world.db, "telegram:4242", [("user", "hi")], person=ALICE)
    first = _hashed_old_fact(world, "The operator prefers dark roast.", "telegram:4242", None)
    second = _hashed_old_fact(world, "The operator prefers dark roast.", "telegram:4242", ALICE)
    report = rescope_conversation_facts(TENANT, apply=True)
    assert report["rescoped"] == 1 and report["retired_duplicates"] == 1
    rows = {f["id"]: f for f in world.db.facts()}
    assert rows[first]["is_active"] and rows[first]["scope_id"] == ALICE_P
    assert not rows[second]["is_active"] and rows[second]["superseded_by"] == first
    assert rescope_conversation_facts(TENANT, apply=True)["examined"] == 0  # the cursor moved


@pytest.mark.asyncio
async def test_a_rescoped_facts_edges_and_nodes_stop_being_public(world: World) -> None:
    from robothor.memory.conversation_backfill import rescope_conversation_facts

    store_chat(world.db, "telegram:4242", [("user", "hi")], person=ALICE)
    world.model.on(
        "OLD1",
        wp.fact(
            "Dana Reyes treats Reader Person at Northside Oncology Clinic.",
            [("Dana Reyes", "person"), ("Northside Oncology Clinic", "organization")],
            [("Dana Reyes", "works at", "Northside Oncology Clinic")],
        ),
    )
    world.model.on(
        "PUB",
        wp.fact(
            "Dana Reyes presented at the staff meeting.",
            [("Dana Reyes", "person")],
        ),
    )
    await wp._ingest(
        "OLD1 text", "telegram", "conversation", role="user",
        metadata={"session_key": "telegram:4242"},
    )  # fmt: skip
    await wp._ingest("PUB notes", "api", "note")  # a shared fact still names Dana Reyes
    old = world.db.fact_id("Dana Reyes treats Reader Person at Northside Oncology Clinic.")
    assert world.db.edges()

    dry = rescope_conversation_facts(TENANT)
    assert dry["edges_removed"] == 1 and dry["nodes_removed"] == 1
    assert world.db.edges()  # a dry run changes nothing

    report = rescope_conversation_facts(TENANT, apply=True)
    assert report["rescoped"] == 1
    assert world.db.edges() == []
    assert world.db.q("SELECT count(*) FROM memory_relation_evidence")[0][0] == 0
    names = {r[0] for r in world.db.q("SELECT name FROM memory_entities")}
    assert "Dana Reyes" in names  # still named by a shared fact
    assert "Northside Oncology Clinic" not in names  # minted from the now-private fact alone
    (row,) = [f for f in world.db.facts() if f["id"] == old]
    assert row["metadata"]["graph_pending_relations"] == [
        {"source": "Dana Reyes", "relation": "works at", "target": "Northside Oncology Clinic"}
    ]
    assert (
        world.db.q(
            "SELECT count(*) FROM memory_resolution_decisions "
            "WHERE surface_form = 'Northside Oncology Clinic'"
        )[0][0]
        == 0
    )
