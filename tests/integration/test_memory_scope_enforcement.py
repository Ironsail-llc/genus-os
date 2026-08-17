"""A restricted identity actually loses a row — on both read paths.

``ROBOTHOR_DATA_SCOPING=enforce`` is live in production. It looks quiet only
because ``tenant_users`` holds two rows and both are ``owner``, which
``scope_for`` treats as unrestricted. The trap arms the moment ``robothor user
add`` creates a single ``member``/``user``/``viewer``/``guest`` row — no flag
flip, no deploy, no review gate.

When it arms, it will do nothing on the primary read path. ``_search_memory``
early-returns into ``_search_memory_routed`` when RIP 15 is on, and that path
never reads ``ctx.identity``, never calls ``data_scoping_mode()``, and never
passes ``scope`` to ``search_facts``. The entire scoping block lives on the
fallback branch that the flag skips.

Observe mode would not catch it either: ``router._normalize_fact`` strips
``person_id``, and ``rows_dropped_by_scope`` counts rows whose ``person_id`` is
neither None nor the caller's — with the field absent every row reads as None,
so the counter reports ``dropped=0``. A clean signal from a control that is
doing nothing.

These tests fire a real violation. The negative control comes first: if an
owner does not see all three rows, the fixture is broken and every later
assertion would pass vacuously.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

_CONSTANT_VECTOR = [0.1] * 1024
_TOKEN = "zarquon"


@pytest.fixture
def _no_ollama(monkeypatch):
    """Stub the embedding service only — an external dependency, not the control.

    A constant vector makes every row tie on the vector leg so BM25 over the
    generated tsv column does the retrieving, and CI needs no Ollama. No stub
    here can make a broken scoping predicate look correct: the assertions read
    which rows come back.
    """
    from robothor.llm import ollama as llm_client

    async def _fake_embedding(_text: str):
        return list(_CONSTANT_VECTOR)

    async def _fake_generate(*_a, **_kw):
        # The cross-encoder reranker also goes through generate(). Stubbing it
        # keeps the test deterministic and Ollama-free; it cannot affect which
        # rows the scoping predicate admits.
        return "no"

    monkeypatch.setattr(llm_client, "get_embedding_async", _fake_embedding)
    monkeypatch.setattr(llm_client, "generate", _fake_generate)


@pytest.fixture
def scoped_corpus(db_cursor, test_prefix):
    """Three facts sharing a token: Alice's, Bob's, and one org-general."""
    tenant = f"{test_prefix}-tenant"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )

    people = {}
    for who in ("alice", "bob"):
        pid = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name, last_name, tenant_id) VALUES (%s, %s, %s, %s)",
            (pid, test_prefix, who, tenant),
        )
        people[who] = pid

    ids = {}
    rows = [
        ("alice", people["alice"], f"{_TOKEN} briefing owned by alice"),
        ("bob", people["bob"], f"{_TOKEN} briefing owned by bob"),
        ("shared", None, f"{_TOKEN} briefing shared across the org"),
    ]
    for label, pid, text in rows:
        db_cursor.execute(
            "INSERT INTO memory_facts "
            "(fact_text, category, tenant_id, is_active, importance_score, person_id, embedding) "
            "VALUES (%s, 'project', %s, TRUE, 0.6, %s, %s::vector) RETURNING id",
            (text, tenant, pid, str(_CONSTANT_VECTOR)),
        )
        ids[label] = db_cursor.fetchone()["id"]

    return {"tenant": tenant, "people": people, "ids": ids}


def _ctx(tenant: str, *, person_id: str | None, role: str):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.identity.context import IdentityContext

    identity = IdentityContext(
        tenant_id=tenant,
        channel="webchat",
        identifier="test-user",
        verified=True,
        role=role,
        person_id=person_id,
    )
    return ToolContext(agent_id="test", tenant_id=tenant, user_id="test-user", identity=identity)


async def _search(query: str, ctx):
    from robothor.engine.tools.handlers.memory import _search_memory

    out = await _search_memory({"query": query, "limit": 10}, ctx)
    return out.get("results") or []


@pytest.mark.parametrize("rip15", ["0", "1"])
@pytest.mark.asyncio
async def test_owner_sees_every_row(
    scoped_corpus, mock_get_connection, _no_ollama, monkeypatch, rip15
):
    """Negative control, and it runs first for a reason.

    Without it, an assertion that "bob's row is absent" would pass just as
    happily against an empty result set, and the suite would certify a scoping
    control that simply returns nothing.
    """
    monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", rip15)
    monkeypatch.setenv("ROBOTHOR_DATA_SCOPING", "enforce")

    ctx = _ctx(scoped_corpus["tenant"], person_id=scoped_corpus["people"]["alice"], role="owner")
    ids = {r.get("id") for r in await _search(_TOKEN, ctx)}

    assert set(scoped_corpus["ids"].values()) <= ids, (
        f"owner should see all three rows; got {ids} of {scoped_corpus['ids']}"
    )


@pytest.mark.parametrize("rip15", ["0", "1"])
@pytest.mark.asyncio
async def test_restricted_identity_loses_another_persons_row(
    scoped_corpus, mock_get_connection, _no_ollama, monkeypatch, rip15
):
    """The violation itself: a member must not see Bob's row.

    Red today with rip15="1" — the routed path never applies scope — and green
    with "0". Parametrizing pins both paths with one assertion, so the routed
    path cannot silently diverge again.
    """
    monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", rip15)
    monkeypatch.setenv("ROBOTHOR_DATA_SCOPING", "enforce")

    ctx = _ctx(scoped_corpus["tenant"], person_id=scoped_corpus["people"]["alice"], role="member")
    ids = {r.get("id") for r in await _search(_TOKEN, ctx)}

    assert scoped_corpus["ids"]["bob"] not in ids, (
        "a restricted identity received another person's row — data scoping is inert"
    )
    assert scoped_corpus["ids"]["alice"] in ids, "own row must remain visible"
    assert scoped_corpus["ids"]["shared"] in ids, "org-general row must remain visible"


@pytest.mark.parametrize("rip15", ["0", "1"])
@pytest.mark.asyncio
async def test_observe_mode_counts_the_row_it_would_drop(
    scoped_corpus, mock_get_connection, _no_ollama, monkeypatch, caplog, rip15
):
    """Observe must report a non-zero would-drop, not a comforting zero.

    This is the assertion that catches the person_id strip specifically: with
    the field missing from normalised rows the counter reads every row as
    org-general and logs dropped=0, which looks exactly like a clean run.
    """
    import logging

    monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", rip15)
    monkeypatch.setenv("ROBOTHOR_DATA_SCOPING", "observe")

    ctx = _ctx(scoped_corpus["tenant"], person_id=scoped_corpus["people"]["alice"], role="member")
    with caplog.at_level(logging.INFO):
        await _search(_TOKEN, ctx)

    drops = [rec.getMessage() for rec in caplog.records if "would_drop" in rec.getMessage()]
    assert drops, "observe mode logged no would-drop line at all"
    assert not any("would_drop=0" in m for m in drops), (
        f"observe reported a zero would-drop while another person's row was "
        f"retrievable — a false clean signal: {drops}"
    )


@pytest.mark.asyncio
async def test_vault_leg_fails_closed_for_restricted_callers(
    scoped_corpus, db_cursor, mock_get_connection, _no_ollama, monkeypatch
):
    """The vault holds credential and PII captions and cannot express person scoping.

    The router added it to the exact_lookup class — a store the path it replaced
    never queried at all. Returning it unfiltered to a restricted caller would be
    a strictly wider exposure than before, so it fails closed. An owner still
    gets it (second negative control), which is what proves the leg is wired and
    the restricted case is not passing for want of data.
    """
    from robothor.memory.router import classify_query, recall

    tenant = scoped_corpus["tenant"]
    query = "what is the account id for zarquon"
    assert classify_query(query) == "exact_lookup", "fixture query no longer hits the vault leg"

    db_cursor.execute(
        "INSERT INTO memory_vault "
        "(entry_type, caption, value_exact, tenant_id, caption_embedding) "
        "VALUES ('account_id', %s, %s, %s, %s::vector)",
        (f"{_TOKEN} account id for the depot", "AC-000-111", tenant, str(_CONSTANT_VECTOR)),
    )

    from robothor.identity.scope import DataScope

    owner_out = await recall(query, tenant_id=tenant, scope=None)
    assert any(r.get("source") == "vault" for r in owner_out["results"]), (
        "vault leg did not fire for an unrestricted caller — the restricted "
        "assertion below would pass vacuously"
    )

    restricted = DataScope(tenant_id=tenant, person_id=scoped_corpus["people"]["alice"], restricted=True)
    out = await recall(query, tenant_id=tenant, scope=restricted)
    assert not any(r.get("source") == "vault" for r in out["results"]), (
        "a restricted caller received vault captions"
    )
