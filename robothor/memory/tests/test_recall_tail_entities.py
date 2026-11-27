"""The budget-filled tail drops rows about a different person who shares a name.

Every loss the budget fill caused on the eval world was a tail row about a
DIFFERENT person or company sharing a name token with the one the question
names ("Wendy Calley" under "Where is Wendy Larkmore employed?"). The tail is
entity-aware: the question's entities are resolved with the identity layer's
seed linking (strong keys or a full name, never one token), and a tail row is
dropped when it is linked to an unnamed person or company entity that shares a
name token with a named one, unless it is also linked to a named entity. The
reranked head is untouched, and with no resolvable entity the tail is as before.

On a database migrated through the whole chain (``test_private_scope_end_to_end``'s
fixture); the reranker's head is one row, so every other row is tail.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.memory.tests.test_private_scope_end_to_end import TENANT, _person, db  # noqa: F401

pytestmark = pytest.mark.integration

LARKMORE = "Wendy Larkmore works at Silix Systems as a support specialist"
CALLEY = "Wendy Calley works at Ulmondo as a staff engineer"
SILIX = "Silix Systems is headquartered in Brelford"
BOTH = "Wendy Larkmore and Wendy Calley share a desk on Fridays"
FILLER = [f"Note {n}: " + " ".join(f"w{n:02d}{k}x" for k in range(5)) for n in range(20)]


def _fact(text: str, *people: str, orgs: tuple[str, ...] = ()) -> dict[str, Any]:
    mentions = [{"surface_form": p, "entity_type": "person"} for p in people]
    mentions += [{"surface_form": o, "entity_type": "organization"} for o in orgs]
    return {
        "fact_text": text,
        "category": "work",
        "confidence": 0.9,
        "entities": [*people, *orgs],
        "mentions": mentions,
    }


@pytest.fixture
async def world(db, monkeypatch) -> dict[str, Any]:  # noqa: F811
    from robothor.memory.write_path import WriteContext, write_facts
    from robothor.rag import reranker

    def _ctx(scope: tuple[str, str] | None = None) -> Any:
        return WriteContext.for_source(
            tenant_id=TENANT,
            source_channel="webchat",
            content_type="conversation",
            role="user",
            scope=scope,
        )

    alice, bob = _person(db, "Alice"), _person(db, "Bob")
    await write_facts(
        [
            _fact(LARKMORE, "Wendy Larkmore", orgs=("Silix Systems",)),
            _fact(CALLEY, "Wendy Calley", orgs=("Ulmondo",)),
            _fact(SILIX, orgs=("Silix Systems",)),
            _fact(BOTH, "Wendy Larkmore", "Wendy Calley"),
            *[{"fact_text": t, "category": "work", "confidence": 0.9} for t in FILLER],
        ],
        source_content="shared notes",
        context=_ctx(),
        conflict_check=False,
    )
    secret = "Wendy Calley's locker code is 7781"
    await write_facts(
        [_fact(secret, "Wendy Calley")],
        source_content="private note",
        context=_ctx(("user", f"person:{alice}")),
        conflict_check=False,
    )
    # Head of one row: everything else is tail.
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "1")
    monkeypatch.setenv("MEMORY_RERANK_MAX_CANDIDATES", "1")
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    monkeypatch.setattr(reranker, "rerank_pair", AsyncMock(return_value=0.5))
    return {"alice": alice, "bob": bob, "secret": secret}


async def _texts(query: str, **kw: Any) -> list[str]:
    from robothor.memory.facts import search_facts

    rows = await search_facts(query, limit=60, tenant_id=TENANT, include_segments=False, **kw)
    return [str(r.get("fact_text")) for r in rows[1:]]  # the tail only


async def test_a_confusable_same_first_name_row_leaves_the_tail(world) -> None:
    tail = await _texts("Where is Wendy Larkmore employed?")
    assert CALLEY not in tail
    assert any(t.startswith("Note ") for t in tail), "unlinked rows stay"


async def test_the_same_row_stays_when_the_question_names_that_person(world) -> None:
    query = "Where is Wendy Calley employed?"
    rows = [*await _head(query), *await _texts(query)]
    assert CALLEY in rows
    assert LARKMORE not in await _texts(query), "now Larkmore is the confusable one"


async def _head(query: str) -> list[str]:
    from robothor.memory.facts import search_facts

    rows = await search_facts(query, limit=60, tenant_id=TENANT, include_segments=False)
    return [str(r.get("fact_text")) for r in rows[:1]]


async def test_a_row_about_a_non_confusable_entity_stays(world) -> None:
    rows = [
        *await _head("Where is Wendy Larkmore employed?"),
        *await _texts("Where is Wendy Larkmore employed?"),
    ]
    assert SILIX in rows, "a bridging row about another, unconfusable entity"
    assert BOTH in rows, "a row also linked to the named person"


async def test_no_named_entity_leaves_the_tail_unchanged(world) -> None:
    rows = [*await _head("Who works where?"), *await _texts("Who works where?")]
    assert CALLEY in rows
    assert LARKMORE in rows


async def test_private_rows_never_appear(world) -> None:
    for query in ("Where is Wendy Calley employed?", "What is the locker code?"):
        rows = [*await _head(query), *await _texts(query, reader_person_id=world["bob"])]
        assert world["secret"] not in rows


async def test_a_confusable_row_the_graph_leg_finds_takes_no_reserved_place(world) -> None:
    """The graph leg's reserved places are unscored rows too: a confusable
    row it reaches (here through the fact both Wendys share) is held to the
    same rule as the tail, whichever leg found it. Only the reranked head is
    exempt."""
    from robothor.memory.facts import search_facts

    for graph in (True, False):
        rows = await search_facts(
            "Where is Wendy Larkmore employed?",
            limit=60,
            tenant_id=TENANT,
            include_segments=False,
            include_graph=graph,
        )
        unscored = [str(r.get("fact_text")) for r in rows if r.get("rerank_score") is None]
        assert CALLEY not in unscored, f"include_graph={graph}"
