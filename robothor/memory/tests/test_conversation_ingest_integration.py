"""Conversation ingest on real Postgres: who said it, whose it is, when it was.

Reuses the write-path integration world (one scratch database built from the
migration chain, strict model fakes). Every scenario stores a conversation the
way chat_store does and ingests it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.memory.graph_schema import Origin
from robothor.memory.tests import test_write_path_integration as wp
from robothor.memory.tests.test_write_path_integration import (
    ALICE,
    ALICE_P,
    TENANT,
    World,
    store_chat,
    turn_fact,
)

# The write-path module's scratch database and world, as fixtures here.
database = wp.database
world = wp.world

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_follow_ups(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from robothor.memory import conversation_ingest as ci

    scheduled: list[Any] = []
    monkeypatch.setattr(ci, "_schedule", lambda delay, fn: scheduled.append((delay, fn)))
    return scheduled


def _history(turns: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [{"role": t[0], "content": t[1]} for t in turns]


async def _ingest(key: str, turns: list[tuple[Any, ...]], trigger: str = "telegram") -> Any:
    from robothor.memory.conversation_ingest import ingest_conversation_session

    return await ingest_conversation_session(key, _history(turns), "main", trigger, "run", TENANT)


def _anchored_to(world: World, fact_id: int) -> list[str | None]:
    return [
        r[0]
        for r in world.db.q(
            "SELECT e.crm_person_id::text FROM memory_mentions m "
            "JOIN memory_entities e ON e.id = m.entity_id WHERE m.fact_id = %s",
            (fact_id,),
        )
    ]


# ═══ only the owner's own turns are the owner's ══════════════════════════


@pytest.mark.asyncio
async def test_a_guest_in_the_main_session_is_never_the_operator(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robothor.memory import segments

    monkeypatch.setattr(segments, "_main_session_key", lambda: "agent:main:primary")
    monkeypatch.setattr(segments, "_owner_person_id", lambda tenant: ALICE)
    world.model.on_turn(
        "penicillin", turn_fact("The user is allergic to penicillin.", about_user=True)
    )
    turns = [
        ("user", "I'm allergic to penicillin", {"author_display_name": "Bob Guest"}),
        ("assistant", "ok"),
    ]
    store_chat(world.db, "agent:main:primary", turns)
    await _ingest("agent:main:primary", turns)
    (row,) = world.db.facts()
    assert row["origin"] == Origin.EXTERNAL
    assert ALICE not in _anchored_to(world, row["id"])
    assert "subject" not in row["metadata"]


@pytest.mark.asyncio
async def test_once_another_person_speaks_no_unmarked_turn_is_the_owners(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robothor.memory import segments

    monkeypatch.setattr(segments, "_main_session_key", lambda: "agent:main:primary")
    monkeypatch.setattr(segments, "_owner_person_id", lambda tenant: ALICE)
    world.model.on_turn("vegetarian", turn_fact("The user is vegetarian.", about_user=True))
    turns = [
        ("user", "hello all", {"author_display_name": "Bob Guest"}),
        ("assistant", "hi"),
        ("user", "I'm vegetarian"),
        ("assistant", "noted"),
    ]
    store_chat(world.db, "agent:main:primary", turns)
    await _ingest("agent:main:primary", turns)
    (row,) = world.db.facts()
    assert row["origin"] == Origin.EXTERNAL
    assert ALICE not in _anchored_to(world, row["id"])


@pytest.mark.asyncio
async def test_a_group_chat_turn_with_no_author_is_not_anyones_own(world: World) -> None:
    world.model.on_turn("allergic", turn_fact("The user is allergic to nuts.", about_user=True))
    turns = [("user", "I'm allergic to nuts"), ("assistant", "ok")]
    store_chat(world.db, "telegram:-100777", turns)
    await _ingest("telegram:-100777", turns)
    (row,) = world.db.facts()
    assert row["origin"] == Origin.EXTERNAL


@pytest.mark.asyncio
async def test_the_owners_own_one_to_one_chat_is_theirs(world: World) -> None:
    world.model.on_turn("allergic", turn_fact("The user is allergic to nuts.", about_user=True))
    turns = [("user", "I'm allergic to nuts", {"sender_telegram_id": "4242"}), ("assistant", "ok")]
    store_chat(world.db, "telegram:4242", turns, person=ALICE)
    await _ingest("telegram:4242", turns)
    (row,) = world.db.facts()
    assert row["origin"] == Origin.USER_STATED
    assert (row["scope_kind"], row["scope_id"]) == ("user", ALICE_P)
    assert ALICE in _anchored_to(world, row["id"])


@pytest.mark.asyncio
async def test_a_members_own_turn_in_their_own_session_is_theirs(world: World) -> None:
    world.model.on_turn("allergic", turn_fact("The user is allergic to nuts.", about_user=True))
    turns = [("user", "I'm allergic to nuts", {"author_user_id": "member-1"}), ("assistant", "ok")]
    store_chat(world.db, "agent:main:user:member-1", turns)
    await _ingest("agent:main:user:member-1", turns, trigger="webchat")
    (row,) = world.db.facts()
    assert row["origin"] == Origin.USER_STATED
    assert row["scope_id"] == "user:member-1"


# ═══ a turn's day is its speaker's day ═══════════════════════════════════


def test_turns_are_read_in_the_instances_timezone(world: World, monkeypatch: Any) -> None:
    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "platform_timezone", lambda: "America/New_York")
    ids = store_chat(world.db, "telegram:4242", [("user", "hi")], person=ALICE)
    conv = ci._load_conversation("telegram:4242", TENANT, 0, 10)
    assert conv is not None
    (turn,) = conv.turns
    assert turn.ref == ids[0]
    assert str(turn.at.tzinfo) == "America/New_York"  # 2023-05-20 05:00 there, 09:00 UTC
    assert turn.at.hour == 5


def test_an_unknown_timezone_falls_back_to_utc(world: World, monkeypatch: Any) -> None:
    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "platform_timezone", lambda: "Not/AZone")
    store_chat(world.db, "telegram:4242", [("user", "hi")], person=ALICE)
    conv = ci._load_conversation("telegram:4242", TENANT, 0, 10)
    assert conv is not None
    assert conv.turns[0].at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


# ═══ the watermark row on a real database ════════════════════════════════


def test_only_one_run_holds_a_conversation_until_it_releases(world: World) -> None:
    from robothor.memory import conversation_ingest as ci

    first = ci._claim("telegram:4242", TENANT)
    assert first is not None and first.through == 0
    assert ci._claim("telegram:4242", TENANT) is None  # another engine process
    ci._advance("telegram:4242", TENANT, first.token, 42, [])
    ci._release("telegram:4242", TENANT, first.token)
    again = ci._claim("telegram:4242", TENANT)
    assert again is not None and again.through == 42
    (raw,) = world.db.q(
        "SELECT content_hash FROM ingested_items WHERE item_id = %s", ("telegram:4242",)
    )[0]
    assert len(raw) <= 64


def test_a_claim_whose_run_died_lapses(world: World, monkeypatch: Any) -> None:
    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "CLAIM_LEASE_SECONDS", -1)
    assert ci._claim("telegram:4242", TENANT) is not None
    assert ci._claim("telegram:4242", TENANT) is not None


def test_a_stale_claim_cannot_move_the_watermark(world: World, monkeypatch: Any) -> None:
    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "CLAIM_LEASE_SECONDS", -1)
    old = ci._claim("telegram:4242", TENANT)
    monkeypatch.setattr(ci, "CLAIM_LEASE_SECONDS", 900)
    new = ci._claim("telegram:4242", TENANT)
    assert old is not None and new is not None
    ci._advance("telegram:4242", TENANT, old.token, 99, [])
    ci._release("telegram:4242", TENANT, new.token)
    assert ci._claim("telegram:4242", TENANT).through == 0  # type: ignore[union-attr]


# ═══ a lower id committed later is still extracted ═══════════════════════


@pytest.mark.asyncio
async def test_a_turn_committed_after_a_later_id_is_not_lost(
    world: World, monkeypatch: Any, _no_follow_ups: list[Any]
) -> None:
    import asyncio

    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "HOLD_SECONDS", 0.5)
    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    key = "telegram:4242"
    sid = world.db.one(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
        "VALUES (%s, %s, 'telegram', %s) RETURNING id",
        (TENANT, key, ALICE),
    )

    def put(i: int, role: str, content: str) -> None:
        world.db.q(
            "INSERT INTO chat_messages (id, session_id, message) VALUES (%s, %s, %s::jsonb)",
            (i, sid, json.dumps({"role": role, "content": content})),
        )

    # Writer B's ids (102, 103) commit first; writer A took 100, 101 earlier.
    put(102, "user", "I have a red road bike.")
    put(103, "assistant", "Nice.")
    await _ingest(key, [("user", "I have a red road bike."), ("assistant", "Nice.")])
    put(100, "user", "I always go for dark roast.")
    put(101, "assistant", "Noted.")
    assert _no_follow_ups  # the fresh turns were left for a follow-up
    await asyncio.sleep(0.6)
    _delay, follow_up = _no_follow_ups[-1]
    await follow_up()
    texts = {f["fact_text"] for f in world.db.facts()}
    assert texts == {"The user prefers dark roast coffee.", "The user owns a red road bike."}


# ═══ a conversation's earlier facts follow it when its owner is learned ══


@pytest.mark.asyncio
async def test_facts_from_before_the_owner_was_known_are_reowned(world: World) -> None:
    """The Telegram first-exchange race: ingest runs before the CRM stamps the person."""
    from robothor.memory.visibility import fact_visibility_sql

    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    key = "telegram:4242"
    t1 = [
        ("user", "I always go for dark roast.", {"sender_telegram_id": "4242"}),
        ("assistant", "Noted."),
    ]
    store_chat(world.db, key, t1)  # no person yet
    await _ingest(key, t1)
    (early,) = world.db.facts()
    assert early["scope_id"] == f"conversation:{key}"
    world.db.q("UPDATE chat_sessions SET person_id = %s WHERE session_key = %s", (ALICE, key))
    # Another tenant's and another conversation's placeholders stay where they are.
    world.db.q(
        "INSERT INTO memory_facts (fact_text, category, tenant_id, scope_kind, scope_id, "
        "metadata) VALUES ('The user likes tea.', 'preference', %s, 'user', %s, '{}')",
        (TENANT, "conversation:telegram:999"),
    )
    t2 = [
        ("user", "I have a red road bike.", {"sender_telegram_id": "4242"}),
        ("assistant", "Nice."),
    ]
    store_chat(world.db, key, t2, person=ALICE)
    await _ingest(key, t1 + t2)

    rows = {f["fact_text"]: f for f in world.db.facts()}
    reowned = rows["The user prefers dark roast coffee."]
    assert (reowned["scope_kind"], reowned["scope_id"]) == ("user", ALICE_P)
    assert reowned["person_id"] == ALICE
    assert reowned["metadata"]["subject"] == {"kind": "person", "person_id": ALICE}
    assert ALICE in _anchored_to(world, reowned["id"])
    assert rows["The user likes tea."]["scope_id"] == "conversation:telegram:999"
    pred, params = fact_visibility_sql([ALICE_P])
    seen = {r[0] for r in world.db.q(f"SELECT fact_text FROM memory_facts WHERE {pred}", params)}
    assert seen == {"The user prefers dark roast coffee.", "The user owns a red road bike."}


@pytest.mark.asyncio
async def test_a_reowned_fact_the_owner_already_has_is_retired_not_duplicated(
    world: World,
) -> None:
    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    key = "telegram:4242"
    t1 = [
        ("user", "I always go for dark roast.", {"sender_telegram_id": "4242"}),
        ("assistant", "Noted."),
    ]
    store_chat(world.db, key, t1)
    await _ingest(key, t1)
    world.db.q("UPDATE chat_sessions SET person_id = %s WHERE session_key = %s", (ALICE, key))
    t2 = [
        ("user", "Still dark roast for me.", {"sender_telegram_id": "4242"}),
        ("assistant", "Got it."),
    ]
    store_chat(world.db, key, t2, person=ALICE)
    await _ingest(key, t1 + t2)
    live = [f for f in world.db.facts() if f["is_active"]]
    assert [(f["fact_text"], f["scope_id"]) for f in live] == [
        ("The user prefers dark roast coffee.", ALICE_P)
    ]


# ═══ history the old path ingested is not extracted again ════════════════


@pytest.mark.asyncio
async def test_a_conversation_the_old_path_ingested_starts_at_its_end(world: World) -> None:
    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    key = "telegram:4242"
    turns = [
        ("user", "I always go for dark roast."),  # 09:00, before the old path ran
        ("assistant", "Noted."),  # 09:01
        ("user", "I have a red road bike."),  # 09:02, after it
        ("assistant", "Nice."),  # 09:03
    ]
    store_chat(world.db, key, turns, person=ALICE)
    world.db.q(
        "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, ingested_at) "
        "VALUES (%s, 'conversation_session', %s, 'old-hash', '2023-05-20T09:01:30Z')",
        (TENANT, key),
    )
    await _ingest(key, turns)
    assert {f["fact_text"] for f in world.db.facts()} == {"The user owns a red road bike."}


@pytest.mark.asyncio
async def test_a_conversation_the_old_path_never_saw_is_extracted_whole(world: World) -> None:
    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    key = "telegram:4242"
    turns = [("user", "I always go for dark roast."), ("assistant", "Noted.")]
    store_chat(world.db, key, turns, person=ALICE)
    await _ingest(key, turns)
    assert {f["fact_text"] for f in world.db.facts()} == {"The user prefers dark roast coffee."}


@pytest.mark.asyncio
async def test_the_old_paths_row_under_the_default_tenant_counts_too(world: World) -> None:
    """The previous ingest recorded its dedup row without a tenant, so under the default."""
    from robothor.constants import DEFAULT_TENANT

    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    world.db.q(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Default') ON CONFLICT DO NOTHING",
        (DEFAULT_TENANT,),
    )
    key = "telegram:4242"
    turns = [("user", "I always go for dark roast."), ("assistant", "Noted.")]
    store_chat(world.db, key, turns, person=ALICE)
    world.db.q(
        "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, ingested_at) "
        "VALUES (%s, 'conversation_session', %s, 'old-hash', '2023-05-20T10:00:00Z')",
        (DEFAULT_TENANT, key),
    )
    await _ingest(key, turns)
    assert world.db.facts() == []


# ═══ a damaged watermark never restarts the conversation ═════════════════


@pytest.mark.parametrize("bad", ["2", "2|x|||", "garbage", "", "9" * 64])
@pytest.mark.asyncio
async def test_a_malformed_watermark_starts_at_the_current_end_and_says_so(
    world: World, bad: str, caplog: Any
) -> None:
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    key = "telegram:4242"
    turns = [
        ("user", "I have a red road bike.", {"sender_telegram_id": "4242"}),
        ("assistant", "Nice."),
    ]
    ids = store_chat(world.db, key, turns, person=ALICE)
    await _ingest(key, turns)
    calls = world.model.calls.count("extract_conversation")
    world.db.q(
        "UPDATE ingested_items SET content_hash = %s WHERE item_id = %s AND "
        "source_name = 'conversation_facts'",
        (bad, key),
    )
    with caplog.at_level("ERROR"):
        await _ingest(key, turns)
    assert world.model.calls.count("extract_conversation") == calls  # history not redone
    assert len(world.db.facts()) == 1
    (state,) = world.db.q(
        "SELECT content_hash FROM ingested_items WHERE item_id = %s AND "
        "source_name = 'conversation_facts'",
        (key,),
    )[0]
    assert state.split("|")[0] == str(ids[-1])
    assert any("malformed" in r.getMessage().lower() for r in caplog.records)


# ═══ a writer open longer than the hold still loses nothing ══════════════


@pytest.mark.asyncio
async def test_a_turn_from_a_writer_open_longer_than_the_hold_is_still_extracted(
    world: World,
) -> None:
    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    key = "telegram:4242"
    sid = world.db.one(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
        "VALUES (%s, %s, 'telegram', %s) RETURNING id",
        (TENANT, key, ALICE),
    )

    def put(i: int, role: str, content: str, ago: int) -> None:
        world.db.q(
            "INSERT INTO chat_messages (id, session_id, message, created_at) "
            "VALUES (%s, %s, %s::jsonb, now() - %s * interval '1 second')",
            (
                i,
                sid,
                json.dumps({"role": role, "content": content, "sender_telegram_id": "4242"}),
                ago,
            ),
        )

    put(102, "user", "I have a red road bike.", 8)
    put(103, "assistant", "Nice.", 8)
    await _ingest(key, [])  # past the hold: extracted, the watermark moves to 103
    put(100, "user", "I always go for dark roast.", 9)  # its writer was open over 5 s
    put(101, "assistant", "Noted.", 9)
    await _ingest(key, [])
    texts = {f["fact_text"] for f in world.db.facts()}
    assert texts == {"The user prefers dark roast coffee.", "The user owns a red road bike."}
    await _ingest(key, [])  # and each is extracted once
    assert world.model.calls.count("extract_conversation") == 2


# ═══ the owner is known by construction, never assumed ═══════════════════


@pytest.fixture
def main_session(monkeypatch: pytest.MonkeyPatch) -> str:
    from robothor.memory import conversation_ingest as ci
    from robothor.memory import segments

    monkeypatch.setattr(segments, "_main_session_key", lambda: "agent:main:primary")
    monkeypatch.setattr(segments, "_owner_person_id", lambda tenant: ALICE)
    monkeypatch.setattr(ci, "_owner_telegram_chat", lambda: "4242")
    monkeypatch.setattr(ci, "_per_user_sessions_on", lambda: True)
    return "agent:main:primary"


async def _one_turn(
    world: World, key: str, extras: dict[str, Any], person: str | None = None
) -> Any:
    world.model.on_turn(
        "shellfish", turn_fact("The user is allergic to shellfish.", about_user=True)
    )
    turns = [("user", "I'm allergic to shellfish", extras), ("assistant", "ok")]
    store_chat(world.db, key, turns, person=person)
    await _ingest(key, turns)
    (row,) = world.db.facts()
    return row


@pytest.mark.asyncio
async def test_a_main_session_turn_stored_the_telegram_way_without_a_sender_is_not_the_operators(
    world: World, main_session: str
) -> None:
    row = await _one_turn(world, main_session, {"message_id": "77"})
    assert row["origin"] == Origin.EXTERNAL
    assert ALICE not in _anchored_to(world, row["id"])


@pytest.mark.asyncio
async def test_the_operators_own_telegram_turn_in_the_main_session_is_theirs(
    world: World, main_session: str
) -> None:
    row = await _one_turn(world, main_session, {"sender_telegram_id": "4242"})
    assert row["origin"] == Origin.USER_STATED
    assert ALICE in _anchored_to(world, row["id"])


@pytest.mark.asyncio
async def test_someone_else_in_the_operators_default_chat_is_not_the_operator(
    world: World, main_session: str
) -> None:
    row = await _one_turn(world, main_session, {"sender_telegram_id": "9999"})
    assert row["origin"] == Origin.EXTERNAL


@pytest.mark.asyncio
async def test_a_main_session_whose_default_chat_is_a_group_has_no_single_sender(
    world: World, main_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "_owner_telegram_chat", lambda: "-100777")
    row = await _one_turn(world, main_session, {"sender_user_id": "member-9"})
    assert row["origin"] == Origin.EXTERNAL


@pytest.mark.asyncio
async def test_a_webchat_owner_turn_in_a_single_sender_main_session_is_theirs(
    world: World, main_session: str
) -> None:
    row = await _one_turn(world, main_session, {"sender_user_id": "operator-account"})
    assert row["origin"] == Origin.USER_STATED


@pytest.mark.asyncio
async def test_a_private_chat_turn_from_its_own_sender_is_theirs_and_no_sender_is_not(
    world: World,
) -> None:
    row = await _one_turn(world, "telegram:4242", {"sender_telegram_id": "4242"}, person=ALICE)
    assert row["origin"] == Origin.USER_STATED
    world.db.q("TRUNCATE memory_mentions, memory_facts, chat_messages, ingested_items CASCADE")
    world.model.turn_extractions.clear()
    row = await _one_turn(world, "telegram:4242", {}, person=ALICE)
    assert row["origin"] == Origin.EXTERNAL


# ═══ late rows are bounded: no re-extraction, no runaway work ════════════


def _session(world: World, key: str) -> int:
    return int(
        world.db.one(
            "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
            "VALUES (%s, %s, 'telegram', %s) RETURNING id",
            (TENANT, key, ALICE),
        )
    )


def _put(world: World, sid: int, i: int, role: str, content: str, ago: int) -> None:
    world.db.q(
        "INSERT INTO chat_messages (id, session_id, message, created_at) "
        "VALUES (%s, %s, %s::jsonb, now() - %s * interval '1 second')",
        (i, sid, json.dumps({"role": role, "content": content, "sender_telegram_id": "4242"}), ago),
    )


@pytest.mark.asyncio
async def test_ids_too_large_for_the_list_are_never_taken_for_late_rows(world: World) -> None:
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    sid = _session(world, "telegram:4242")
    big = 3_000_000_000
    _put(world, sid, big, "user", "I have a red road bike.", 20)
    _put(world, sid, big + 1, "assistant", "Nice.", 20)
    for _ in range(4):
        await _ingest("telegram:4242", [])
    assert world.model.calls.count("extract_conversation") == 1


@pytest.mark.asyncio
async def test_a_burst_longer_than_the_list_drains_once_and_stops(world: World) -> None:
    from robothor.memory import conversation_ingest as ci

    sid = _session(world, "telegram:4242")
    for i in range(1, 1001):
        _put(world, sid, i, "user" if i % 2 else "assistant", f"m{i}", 30)
    for _ in range(12):
        await _ingest("telegram:4242", [])
    drained = world.model.calls.count("extract_conversation")
    assert drained <= 1000 // ci.WINDOW_MAX_TURNS + 2  # each window once, no re-extraction
    for _ in range(3):
        await _ingest("telegram:4242", [])
    assert world.model.calls.count("extract_conversation") == drained  # nothing new: no calls


@pytest.mark.asyncio
async def test_late_work_per_run_is_capped(world: World, monkeypatch: Any) -> None:
    from robothor.memory import conversation_ingest as ci

    monkeypatch.setattr(ci, "LATE_MAX_PER_RUN", 4)
    sid = _session(world, "telegram:4242")
    _put(world, sid, 200, "user", "first", 30)
    _put(world, sid, 201, "assistant", "ok", 30)
    await _ingest("telegram:4242", [])  # watermark at 201
    for i in range(100, 110):  # ten rows a slow writer committed late
        _put(world, sid, i, "user" if i % 2 == 0 else "assistant", f"late {i}", 30)
    before = world.model.calls.count("extract_conversation")
    await _ingest("telegram:4242", [])
    (raw,) = world.db.q(
        "SELECT fact_ids FROM ingested_items WHERE item_id = 'telegram:4242' "
        "AND source_name = 'conversation_facts'"
    )[0]
    assert len([i for i in raw if 100 <= i < 110]) == 4  # four taken this run
    assert world.model.calls.count("extract_conversation") == before + 1


# ═══ the watermark outlives retention, and a lost one never restarts at 0 ═


def test_retention_keeps_conversation_watermarks_and_skips(world: World, monkeypatch: Any) -> None:
    from robothor.db import connection
    from robothor.engine import retention
    from robothor.memory import write_path

    monkeypatch.setattr(connection, "get_connection", write_path.get_connection)
    for source in (
        "conversation_facts",
        "conversation_facts_skipped",
        "conversation_facts_backfill",
        "email",
    ):
        world.db.q(
            "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, "
            "ingested_at) VALUES (%s, %s, 'x', 'h', now() - interval '200 days')",
            (TENANT, source),
        )
    policy = retention.RETENTION_POLICY["ingested_items"]
    retention._cleanup_table(
        "ingested_items",
        days=policy["days"],
        timestamp_col=policy["timestamp_col"],
        extra_where=policy.get("extra_where"),
    )
    left = {r[0] for r in world.db.q("SELECT source_name FROM ingested_items")}
    assert left == {
        "conversation_facts",
        "conversation_facts_skipped",
        "conversation_facts_backfill",
    }


@pytest.mark.asyncio
async def test_a_conversation_with_no_row_starts_past_turns_older_than_retention(
    world: World,
) -> None:
    world.model.on_turn(
        "dark roast", turn_fact("The user prefers dark roast coffee.", about_user=True)
    )
    world.model.on_turn("road bike", turn_fact("The user owns a red road bike.", about_user=True))
    sid = _session(world, "telegram:4242")
    day = 24 * 3600
    _put(world, sid, 1, "user", "I always go for dark roast.", 120 * day)  # kept only
    _put(world, sid, 2, "assistant", "Noted.", 120 * day)  # because an old fact quoted it
    _put(world, sid, 3, "user", "I have a red road bike.", 60)
    _put(world, sid, 4, "assistant", "Nice.", 60)
    await _ingest("telegram:4242", [])
    assert {f["fact_text"] for f in world.db.facts()} == {"The user owns a red road bike."}
