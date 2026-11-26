"""The entity-aware tail leaves out only what the diagnosis supports.

The losses it addresses were all people sharing a given or family name. A
shared generic company word ("Systems", "Inc"), an honorific in an alias
("Dr."), or a name whose type is unknown sharing a token with a company
("New York" under "New Balance") is not a confusable: those rows stay.
Two companies sharing a distinctive token ("Acme Robotics" under "Acme
Systems Inc") still confuse.

Same fixture as ``test_recall_tail_entities.py``: a one-row reranked head.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.memory.tests.test_private_scope_end_to_end import TENANT, _sql, db  # noqa: F401

pytestmark = pytest.mark.integration

ACME = "Acme Systems Inc builds warehouse conveyor controllers"
GLOBEX = "Globex Systems sells payroll software"
INITECH = "Initech Inc runs printer repair contracts"
ACME_ROBOTICS = "Acme Robotics builds welding arms"
LARKMORE = "Wendy Larkmore leads the clinic research desk"
SMITH = "Dr. Smith runs the night clinic"
NEW_BALANCE = "New Balance opened a running store"
NEW_YORK = "The flagship opened in New York last spring"
FILLER = [f"Note {n}: " + " ".join(f"z{n:02d}{k}q" for k in range(5)) for n in range(15)]


def _fact(text: str, *, people: tuple[str, ...] = (), orgs: tuple[str, ...] = ()) -> dict[str, Any]:
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
async def world(db, monkeypatch) -> None:  # noqa: F811
    from robothor.memory.write_path import WriteContext, write_facts
    from robothor.rag import reranker

    await write_facts(
        [
            _fact(ACME, orgs=("Acme Systems Inc",)),
            _fact(GLOBEX, orgs=("Globex Systems",)),
            _fact(INITECH, orgs=("Initech Inc",)),
            _fact(ACME_ROBOTICS, orgs=("Acme Robotics",)),
            _fact(LARKMORE, people=("Wendy Larkmore",)),
            _fact(SMITH, people=("Dr. Smith",)),
            _fact(NEW_BALANCE, orgs=("New Balance",)),
            # Type unknown: an entities name with no mention.
            {
                "fact_text": NEW_YORK,
                "category": "work",
                "confidence": 0.9,
                "entities": ["New York"],
            },
            *[{"fact_text": t, "category": "work", "confidence": 0.9} for t in FILLER],
        ],
        source_content="shared notes",
        context=WriteContext.for_source(
            tenant_id=TENANT, source_channel="webchat", content_type="conversation", role="user"
        ),
        conflict_check=False,
    )
    _sql(
        db,
        "UPDATE memory_entities SET aliases = ARRAY['Dr. Larkmore'] "
        "WHERE tenant_id = %s AND name = 'Wendy Larkmore'",
        (TENANT,),
    )
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "1")
    monkeypatch.setenv("MEMORY_RERANK_MAX_CANDIDATES", "1")
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    monkeypatch.setattr(reranker, "rerank_pair", AsyncMock(return_value=0.5))


async def _returned(query: str) -> list[str]:
    from robothor.memory.facts import search_facts

    rows = await search_facts(query, limit=60, tenant_id=TENANT, include_segments=False)
    return [str(r.get("fact_text")) for r in rows]


async def test_a_generic_company_word_is_not_a_confusable(world) -> None:
    rows = await _returned("What does Acme Systems Inc make?")
    assert GLOBEX in rows, "'Systems' is a generic company word"
    assert INITECH in rows, "'Inc' is a generic company word"


async def test_a_distinctive_company_token_still_confuses(world) -> None:
    rows = await _returned("What does Acme Systems Inc make?")
    assert ACME_ROBOTICS not in rows[1:]


async def test_an_honorific_in_an_alias_is_not_a_confusable(world) -> None:
    rows = await _returned("What does Wendy Larkmore lead?")
    assert SMITH in rows, "'Dr.' from the alias 'Dr. Larkmore' confuses nobody"


async def test_a_name_of_unknown_type_never_confuses_a_company(world) -> None:
    rows = await _returned("Where did New Balance open a store?")
    assert NEW_YORK in rows
