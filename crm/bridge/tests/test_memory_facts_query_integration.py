"""What the Memory routes actually do to the database.

Separate from ``test_memory_facts_router.py`` for the reason the runs/fleet
suites are split the same way: the unit lane runs with no database at all, so
the SQL it cannot execute is the half most worth executing somewhere. Every
test here writes real rows into the test database, under tenants it creates and
deletes itself.

The claims that need a real table under them:

* a fact in ANOTHER tenant is a 404, not a 403 and never a row;
* preview changes no row and no ``is_active``;
* forget bounds the fact (``is_active=false``, ``valid_to`` set) and nothing else;
* the second forget is a 409;
* the audit row carries the id and the reason, and never the text.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.integration

FACTS = "/api/memory/facts"

#: Two tenants the operator is NOT: one is the platform tenant the gate admits,
#: the other is where the fact that must be invisible lives.
OTHER_TENANT = "__b14a_other__"


@pytest.fixture
def tenants(db_conn):
    """A throwaway tenant beside the platform one, removed afterwards."""
    from routers._operator import PLATFORM_TENANT

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (OTHER_TENANT, "B14a other tenant"),
        )
    db_conn.commit()
    yield PLATFORM_TENANT, OTHER_TENANT
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM memory_facts WHERE tenant_id = %s", (OTHER_TENANT,))
        cur.execute("DELETE FROM crm_tenants WHERE id = %s", (OTHER_TENANT,))
    db_conn.commit()


@pytest.fixture
def make_fact(db_conn, tenants):
    """Insert a fact and return its id; every fact made here is deleted after."""
    created: list[int] = []

    def _make(text: str, *, tenant: str, entities: list[str] | None = None, active: bool = True):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_facts (fact_text, category, entities, tenant_id, is_active) "
                "VALUES (%s, 'fact', %s, %s, %s) RETURNING id",
                (text, entities or [], tenant, active),
            )
            fact_id = int(cur.fetchone()[0])
        db_conn.commit()
        created.append(fact_id)
        return fact_id

    yield _make

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM memory_episodes WHERE fact_ids && %s", (created,))
        cur.execute("DELETE FROM memory_facts WHERE id = ANY(%s)", (created,))
    db_conn.commit()


@pytest.fixture
def live_db(monkeypatch, db_conn):
    """Point the router at the test connection.

    Deliberately WITHOUT the commit the real ``get_connection`` does on exit:
    every write a route makes here stays inside ``db_conn``'s transaction, so
    it is visible to the next request in the same test (which is what makes the
    double-forget 409 a real test) and gone at teardown. The rows the fixtures
    above create ARE committed, and they delete them by id afterwards.
    """
    from routers import memory_facts

    @contextmanager
    def _conn():
        yield db_conn

    monkeypatch.setattr(memory_facts, "get_connection", _conn)


@pytest.fixture
def audit_rows(monkeypatch):
    """Every audit event the routes write, captured at the one sink they use."""
    rows: list[dict] = []

    def _log_event(event_type, **kwargs):
        rows.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr("routers._audit.log_event", _log_event)
    return rows


def _row(db_conn, fact_id: int) -> dict:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT is_active, valid_to, fact_text FROM memory_facts WHERE id = %s", (fact_id,)
        )
        is_active, valid_to, fact_text = cur.fetchone()
    return {"is_active": is_active, "valid_to": valid_to, "fact_text": fact_text}


# ── listing ──────────────────────────────────────────────────────────────────


def test_listing_shows_only_the_callers_tenant(
    controls_client_as_operator, live_db, make_fact, tenants
):
    platform, other = tenants
    marker = uuid.uuid4().hex
    mine = make_fact(f"Alice prefers tea {marker}", tenant=platform)
    make_fact(f"Bob prefers coffee {marker}", tenant=other)

    body = controls_client_as_operator.get(f"{FACTS}?limit=200").json()
    texts = [f["fact_text"] for f in body["facts"]]
    assert any(str(mine) == str(f["id"]) for f in body["facts"])
    assert not any("Bob prefers coffee" in t for t in texts)


def test_listing_is_newest_first_and_pages_by_keyset(
    controls_client_as_operator, live_db, make_fact, tenants
):
    platform, _ = tenants
    marker = uuid.uuid4().hex
    ids = [make_fact(f"Alice fact {n} {marker}", tenant=platform) for n in range(3)]

    first = controls_client_as_operator.get(f"{FACTS}?limit=1").json()
    assert [f["id"] for f in first["facts"]] == [ids[-1]]
    assert first["next_cursor"] == str(ids[-1])

    second = controls_client_as_operator.get(f"{FACTS}?limit=1&cursor={ids[-1]}").json()
    assert [f["id"] for f in second["facts"]] == [ids[-2]]


def test_active_filter_selects_the_three_populations(
    controls_client_as_operator, live_db, make_fact, tenants
):
    platform, _ = tenants
    marker = uuid.uuid4().hex
    live = make_fact(f"Alice is here {marker}", tenant=platform)
    dead = make_fact(f"Alice was here {marker}", tenant=platform, active=False)

    def ids(active: str) -> set[int]:
        body = controls_client_as_operator.get(f"{FACTS}?limit=200&active={active}").json()
        return {f["id"] for f in body["facts"]}

    assert live in ids("true") and dead not in ids("true")
    assert dead in ids("false") and live not in ids("false")
    assert {live, dead} <= ids("all")


def test_entity_filter_is_case_insensitive(
    controls_client_as_operator, live_db, make_fact, tenants
):
    platform, _ = tenants
    marker = uuid.uuid4().hex
    tagged = make_fact(f"Alice ships it {marker}", tenant=platform, entities=["Alice"])

    body = controls_client_as_operator.get(f"{FACTS}?limit=200&entity=alice").json()
    assert tagged in {f["id"] for f in body["facts"]}


def test_a_query_reuses_the_search_ranking_rather_than_inventing_one(
    controls_client_as_operator, live_db, make_fact, tenants, monkeypatch
):
    """``q`` must go through ``robothor.memory.facts.search_facts``.

    A second ranking in the bridge is a second answer to "what does this
    instance consider relevant", and the two would drift the first time either
    was tuned. Asserted by patching the search function itself.
    """
    platform, _ = tenants
    marker = uuid.uuid4().hex
    hit = make_fact(f"Alice runs the standup {marker}", tenant=platform)
    make_fact(f"Bob runs nothing {marker}", tenant=platform)
    seen: dict = {}

    async def _fake_search(query, **kwargs):
        seen.update({"query": query, **kwargs})
        return [{"id": hit}]

    monkeypatch.setattr("robothor.memory.facts.search_facts", _fake_search)
    body = controls_client_as_operator.get(f"{FACTS}?q=standup&limit=5").json()

    assert seen["query"] == "standup"
    assert seen["tenant_id"] == platform
    assert [f["id"] for f in body["facts"]] == [hit]
    assert body["facts"][0]["fact_text"].startswith("Alice runs the standup")


def test_a_query_still_honours_the_entity_filter(
    controls_client_as_operator, live_db, make_fact, tenants, monkeypatch
):
    """A filter the page is still rendering must not be silently dropped.

    The Memory page has an entity chip. Typing a query with the chip set used
    to return facts from every entity under a filter that still looked applied
    — the search arm never saw ``entity`` and the re-read did not filter on it.
    """
    platform, _ = tenants
    marker = uuid.uuid4().hex
    tagged = make_fact(f"Alice runs standup {marker}", tenant=platform, entities=["Alice"])
    untagged = make_fact(f"Bob runs standup {marker}", tenant=platform, entities=["Bob"])

    async def _fake_search(query, **kwargs):
        return [{"id": tagged}, {"id": untagged}]

    monkeypatch.setattr("robothor.memory.facts.search_facts", _fake_search)
    body = controls_client_as_operator.get(f"{FACTS}?q=standup&entity=alice").json()

    assert [f["id"] for f in body["facts"]] == [tagged]


def test_a_query_still_honours_the_tenant_and_active_filters(
    controls_client_as_operator, live_db, make_fact, tenants, monkeypatch
):
    """Whatever ``search_facts`` hands back, the re-read is the gate.

    Asserted with a deliberately over-broad fake: a ranking that returned
    another tenant's fact, or an inactive one under ``active=true``, must not
    put either on the page.
    """
    platform, other = tenants
    marker = uuid.uuid4().hex
    mine = make_fact(f"Alice ships {marker}", tenant=platform)
    inactive = make_fact(f"Alice shipped {marker}", tenant=platform, active=False)
    theirs = make_fact(f"Bob ships {marker}", tenant=other)

    async def _fake_search(query, **kwargs):
        return [{"id": mine}, {"id": inactive}, {"id": theirs}]

    monkeypatch.setattr("robothor.memory.facts.search_facts", _fake_search)
    body = controls_client_as_operator.get(f"{FACTS}?q=ships").json()

    assert [f["id"] for f in body["facts"]] == [mine]


# ── preview ──────────────────────────────────────────────────────────────────


def test_preview_writes_nothing(controls_client_as_operator, live_db, make_fact, tenants, db_conn):
    platform, _ = tenants
    fact_id = make_fact("Alice prefers tea", tenant=platform)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_facts")
        before = cur.fetchone()[0]

    body = controls_client_as_operator.post(f"{FACTS}/{fact_id}/forget/preview").json()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_facts")
        assert cur.fetchone()[0] == before
    assert _row(db_conn, fact_id)["is_active"] is True
    assert body["already_inactive"] is False
    assert body["would_deactivate"] == [fact_id]
    assert body["fact"]["id"] == fact_id


def test_preview_counts_what_cites_the_fact(
    controls_client_as_operator, live_db, make_fact, tenants, db_conn
):
    platform, _ = tenants
    marker = uuid.uuid4().hex
    text = f"Alice prefers tea {marker}"
    fact_id = make_fact(text, tenant=platform, entities=["Alice"])
    block = f"__b14a_block_{marker}"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_episodes "
            "(tenant_id, start_time, end_time, title, summary, fact_ids) "
            "VALUES (%s, now(), now(), 'ep', 'ep', %s)",
            (platform, [fact_id]),
        )
        cur.execute(
            "INSERT INTO agent_memory_blocks (block_name, content, tenant_id) VALUES (%s, %s, %s)",
            (block, f"context: {text}", platform),
        )
    db_conn.commit()
    try:
        body = controls_client_as_operator.post(f"{FACTS}/{fact_id}/forget/preview").json()
        assert body["references"]["entities"] == ["Alice"]
        assert body["references"]["episodes"] == 1
        assert body["references"]["blocks"] == [block]
        assert body["references"]["blocks_scanned"] is True
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM agent_memory_blocks WHERE block_name = %s", (block,))
        db_conn.commit()


def test_a_fact_too_short_to_match_anything_meaningfully_is_not_scanned(
    controls_client_as_operator, live_db, make_fact, tenants, db_conn
):
    """``strpos(content, '') > 0`` is TRUE for every row.

    So an empty or near-empty ``fact_text`` reported every memory block on the
    instance as citing it — and a two-word fact matched any block containing
    those two words in any order of clauses. The module calls ``blocks`` the
    reference an operator cannot find any other way, and a warning that cries
    wolf is the one that gets ignored.
    """
    platform, _ = tenants
    block = f"__b14a_block_{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_memory_blocks (block_name, content, tenant_id) VALUES (%s, %s, %s)",
            (block, "a block with some content in it", platform),
        )
    db_conn.commit()
    try:
        for text in ("", "   ", "tea"):
            fact_id = make_fact(text, tenant=platform)
            references = controls_client_as_operator.post(
                f"{FACTS}/{fact_id}/forget/preview"
            ).json()["references"]
            assert references["blocks"] == [], text
            assert references["blocks_scanned"] is False, text
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM agent_memory_blocks WHERE block_name = %s", (block,))
        db_conn.commit()


def test_preview_of_an_inactive_fact_says_so(
    controls_client_as_operator, live_db, make_fact, tenants
):
    platform, _ = tenants
    fact_id = make_fact("Alice moved on", tenant=platform, active=False)
    body = controls_client_as_operator.post(f"{FACTS}/{fact_id}/forget/preview").json()
    assert body["already_inactive"] is True
    assert body["would_deactivate"] == []


def test_preview_of_another_tenants_fact_is_404(
    controls_client_as_operator, live_db, make_fact, tenants
):
    _, other = tenants
    fact_id = make_fact("Bob's private note", tenant=other)
    resp = controls_client_as_operator.post(f"{FACTS}/{fact_id}/forget/preview")
    assert resp.status_code == 404
    assert "Bob" not in resp.text


# ── forget ───────────────────────────────────────────────────────────────────


def test_forget_bounds_the_fact_and_audits_the_id_not_the_text(
    controls_client_as_operator, live_db, make_fact, tenants, db_conn, audit_rows
):
    platform, _ = tenants
    text = "Alice prefers tea, which is nobody else's business"
    fact_id = make_fact(text, tenant=platform)

    resp = controls_client_as_operator.post(
        f"{FACTS}/{fact_id}/forget", json={"reason": "operator asked to forget it"}
    )
    assert resp.status_code == 200

    row = _row(db_conn, fact_id)
    assert row["is_active"] is False
    assert row["valid_to"] is not None
    assert row["fact_text"] == text, "forget bounds a fact; it does not rewrite it"

    assert [r["event_type"] for r in audit_rows] == ["memory.forget"]
    details = audit_rows[0]["details"]
    assert details["fact_id"] == fact_id
    assert details["reason"] == "operator asked to forget it"
    assert "prefers tea" not in str(audit_rows[0])


def test_forget_touches_only_the_named_fact(
    controls_client_as_operator, live_db, make_fact, tenants, db_conn
):
    """Supersession chains are not followed: the neighbour stays believed."""
    platform, _ = tenants
    target = make_fact("Alice prefers tea", tenant=platform)
    neighbour = make_fact("Alice prefers tea in the morning", tenant=platform)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE memory_facts SET superseded_by = %s WHERE id = %s", (target, neighbour))
    db_conn.commit()

    controls_client_as_operator.post(f"{FACTS}/{target}/forget", json={"reason": "duplicate"})
    assert _row(db_conn, neighbour)["is_active"] is True


def test_the_second_forget_is_a_409(controls_client_as_operator, live_db, make_fact, tenants):
    platform, _ = tenants
    fact_id = make_fact("Alice prefers tea", tenant=platform)
    assert (
        controls_client_as_operator.post(
            f"{FACTS}/{fact_id}/forget", json={"reason": "first time"}
        ).status_code
        == 200
    )
    again = controls_client_as_operator.post(
        f"{FACTS}/{fact_id}/forget", json={"reason": "second time"}
    )
    assert again.status_code == 409
    assert again.json()["detail"].strip().endswith(".")


def test_forgetting_another_tenants_fact_is_404_and_changes_nothing(
    controls_client_as_operator, live_db, make_fact, tenants, db_conn
):
    _, other = tenants
    fact_id = make_fact("Bob's private note", tenant=other)
    resp = controls_client_as_operator.post(f"{FACTS}/{fact_id}/forget", json={"reason": "nosy"})
    assert resp.status_code == 404
    assert _row(db_conn, fact_id)["is_active"] is True


def test_forgetting_a_fact_that_does_not_exist_is_404(controls_client_as_operator, live_db):
    resp = controls_client_as_operator.post(
        f"{FACTS}/2147483647/forget", json={"reason": "not there"}
    )
    assert resp.status_code == 404


def test_the_forget_is_committed_and_visible_to_another_connection(
    controls_client_as_operator, make_fact, tenants, db_conn
):
    """The one test in this file that does NOT bind ``live_db``.

    Every other test here uses a non-committing stand-in, which is what makes
    the double-forget 409 real — both requests then share one transaction. But
    it also means a regression that dropped the commit would leave this whole
    file green, and the commit is the most consequential statement in the
    module. So this one goes through the module's OWN ``get_connection``, the
    production seam that commits on a clean exit, and reads the row back on a
    SECOND connection, where an uncommitted write is invisible by construction.

    ``make_fact`` deletes the row by id afterwards, so the committed write is
    cleaned up rather than left in the test database.
    """
    import psycopg2

    platform, _ = tenants
    fact_id = make_fact("Alice prefers tea, and the write must survive", tenant=platform)

    assert (
        controls_client_as_operator.post(
            f"{FACTS}/{fact_id}/forget", json={"reason": "it must persist"}
        ).status_code
        == 200
    )

    elsewhere = psycopg2.connect(dbname=db_conn.get_dsn_parameters()["dbname"])
    try:
        with elsewhere.cursor() as cur:
            cur.execute("SELECT is_active, valid_to FROM memory_facts WHERE id = %s", (fact_id,))
            is_active, valid_to = cur.fetchone()
        assert is_active is False
        assert valid_to is not None
    finally:
        elsewhere.rollback()
        elsewhere.close()
