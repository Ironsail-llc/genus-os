"""Conversation segmentation: raw turns split into topically coherent segments.

The segment is the unit raw-conversation recall returns. It has to be
contiguous (it names a turn range), topical (one subject, so a hit reads as an
answer), bounded (it fits a reranker and a context budget) and never trivial
(a lone "thanks" is not worth a row). No LLM call: the rule is time gaps,
embedding similarity between adjacent turn windows, and size.

Embeddings are faked deterministically: a bag-of-words hash, so turns that
share vocabulary are similar and turns that do not are orthogonal.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime, timedelta

import pytest

from robothor.memory import segments
from robothor.memory.segments import Segment, Turn, segment_turns

BASE = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "is",
        "it",
        "to",
        "of",
        "and",
        "for",
        "on",
        "at",
        "in",
        "my",
        "i",
        "you",
        "we",
        "what",
        "do",
        "did",
        "can",
    ]
)


def fake_embed(text: str) -> list[float]:
    vec = [0.0] * 1024
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in _STOP:
            continue
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 1024] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def fake_embed_batch(texts: list[str]) -> list[list[float] | None]:
    return [fake_embed(t) for t in texts]


BIKE = [
    ("user", "Where is the bike locker at the garage, and what is the bike locker code?"),
    ("assistant", "The bike locker in the garage uses code 4417; the bike locker is by the lift."),
    ("user", "Does the bike locker code for the garage change monthly?"),
    ("assistant", "The garage bike locker code changes each quarter, so 4417 holds until March."),
]
DENTIST = [
    ("user", "Book the dentist appointment for Tuesday morning with the dentist clinic."),
    ("assistant", "Dentist appointment booked: Tuesday morning, dentist clinic on Elm Street."),
    ("user", "Remind me about the Tuesday dentist appointment at the dentist clinic."),
    ("assistant", "Reminder set: dentist appointment Tuesday morning, dentist clinic, Elm Street."),
]


def turns_of(pairs, *, start_ref: int = 1, start_at: datetime = BASE, embedded: bool = True):
    out = []
    for i, (role, text) in enumerate(pairs):
        out.append(
            Turn(
                ref=start_ref + i,
                role=role,
                text=text,
                at=start_at + timedelta(minutes=i),
                embedding=fake_embed(text) if embedded else None,
            )
        )
    return out


def ranges(segs: list[Segment]) -> list[tuple[int, int]]:
    return [(s.start_ref, s.end_ref) for s in segs]


# ── the acceptance criterion ───────────────────────────────────────────────


def test_two_topics_become_two_segments_with_their_turn_ranges():
    turns = turns_of(BIKE + DENTIST)
    segs = segment_turns(turns)
    assert ranges(segs) == [(1, 4), (5, 8)]
    assert "4417" in segs[0].text and "dentist" not in segs[0].text.lower()
    assert "Tuesday" in segs[1].text and "4417" not in segs[1].text


def test_each_segment_carries_the_event_time_of_its_first_and_last_turn():
    turns = turns_of(BIKE + DENTIST)
    first, second = segment_turns(turns)
    assert first.started_at == turns[0].at and first.ended_at == turns[3].at
    assert second.started_at == turns[4].at and second.ended_at == turns[7].at


def test_segment_text_labels_each_turn_with_its_role():
    seg = segment_turns(turns_of(BIKE))[0]
    lines = seg.text.splitlines()
    assert lines[0].startswith("User: Where is the bike locker")
    assert lines[1].startswith("Assistant: The bike locker")
    assert len(lines) == 4


def test_a_segment_has_a_short_topic_label():
    seg = segment_turns(turns_of(BIKE))[0]
    assert seg.topic
    assert len(seg.topic) <= segments.SEGMENT_TOPIC_MAX_CHARS
    assert "bike locker" in seg.topic.lower()


# ── the three break rules ──────────────────────────────────────────────────


def test_a_time_gap_starts_a_new_segment_even_on_the_same_topic():
    first = turns_of(BIKE)
    later = turns_of(
        BIKE,
        start_ref=5,
        start_at=BASE + timedelta(seconds=segments.SEGMENT_GAP_SECONDS + 3600),
    )
    assert ranges(segment_turns(first + later)) == [(1, 4), (5, 8)]


def test_a_segment_never_exceeds_the_size_budget():
    long_turn = "The bike locker code at the garage is 4417 and the bike locker sits by the lift. "
    pairs = [("user" if i % 2 == 0 else "assistant", long_turn * 3) for i in range(40)]
    segs = segment_turns(turns_of(pairs))
    assert len(segs) > 1
    for seg in segs:
        assert len(seg.text) <= segments.SEGMENT_MAX_CHARS
        assert len(seg.turns) <= segments.SEGMENT_MAX_TURNS


def test_segments_cover_every_turn_contiguously_and_in_order():
    turns = turns_of(BIKE + DENTIST + BIKE)
    segs = segment_turns(turns)
    covered = [t.ref for s in segs for t in s.turns]
    assert covered == [t.ref for t in turns]
    for prev, nxt in zip(segs, segs[1:], strict=False):
        assert nxt.start_ref == prev.end_ref + 1


def test_a_topic_break_falls_only_before_a_user_turn():
    segs = segment_turns(turns_of(BIKE + DENTIST + BIKE))
    for seg in segs[1:]:
        assert seg.turns[0].role == "user"


# ── a turn longer than the budget ──────────────────────────────────────────


def long_text() -> str:
    """About 11,000 characters: paragraphs of sentences, plus one unbroken run."""
    para = " ".join(
        f"Sentence {i} explains how the bike locker rota works for the garage on floor {i}."
        for i in range(25)
    )
    paragraphs = [f"Paragraph {n}. {para}" for n in range(4)]
    return "\n\n".join(paragraphs) + "\n\n" + "x" * 2500


def test_a_long_turn_is_split_across_parts_and_never_clipped():
    original = long_text()
    turn = Turn(ref=7, role="user", text=original, at=BASE)

    segs = segment_turns([turn])

    assert len(segs) > 1
    assert [s.part for s in segs] == list(range(len(segs)))
    assert all((s.start_ref, s.end_ref) == (7, 7) for s in segs)
    assert all(len(s.text) <= segments.SEGMENT_MAX_CHARS for s in segs)
    assert "".join(s.text.removeprefix("User: ") for s in segs) == original


def test_a_long_turn_splits_at_paragraph_or_sentence_boundaries():
    segs = segment_turns([Turn(ref=7, role="user", text=long_text(), at=BASE)])
    assert len(segs) >= 3
    # Every piece but the unbroken run ends where a paragraph or sentence does.
    for seg in segs[:-1]:
        body = seg.text.removeprefix("User: ")
        if "x" * 50 in body:
            continue
        assert body.endswith(("\n\n", ". ", ".")), repr(body[-20:])


def test_the_pieces_of_a_long_turn_share_no_segment_with_other_turns():
    turns = [
        *turns_of(BIKE[:2]),
        Turn(ref=3, role="user", text=long_text(), at=BASE + timedelta(minutes=3)),
        Turn(ref=4, role="assistant", text="ok", at=BASE + timedelta(minutes=4)),
    ]
    segs = segment_turns(turns)
    for seg in segs:
        refs = {t.ref for t in seg.turns}
        assert refs == {3} or 3 not in refs
    assert [t.ref for s in segs for t in s.turns if t.ref != 3] == [1, 2, 4]


def test_an_ordinary_segment_is_part_zero():
    assert {s.part for s in segment_turns(turns_of(BIKE + DENTIST))} == {0}


# ── who may read a conversation (fails closed) ─────────────────────────────

_ROOM = "3f1c2a7e-0d4b-4c1e-9a55-6b0f2d9e8c11"
_ALICE_PERSON = "8a6a5b0e-2f0c-4b1d-9f7e-1c2d3e4f5a6b"
_OWNER_PERSON = "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d"


@pytest.mark.parametrize(
    ("key", "person", "owner", "expected"),
    [
        # A workspace is the only conversation known to be shared.
        (f"agent:main:room:{_ROOM}", None, None, ("shared", None)),
        (f"agent:main:room:{_ROOM}", _ALICE_PERSON, None, ("shared", None)),
        # A session that records its person belongs to that person.
        ("telegram:4242", _ALICE_PERSON, None, ("user", f"person:{_ALICE_PERSON}")),
        # A member's webchat session belongs to the user named in its key.
        ("agent:main:user:alice-uid", None, None, ("user", "user:alice-uid")),
        ("agent:research:user:alice-uid", None, None, ("user", "user:alice-uid")),
        # The main session belongs to the operator.
        ("agent:main:primary", None, _OWNER_PERSON, ("user", f"person:{_OWNER_PERSON}")),
        # No owner can be named: private to nobody, never shared.
        ("agent:main:primary", None, None, ("user", None)),
        ("telegram:4242", None, None, ("user", None)),
        ("agent:main:tui-alice", None, None, ("user", None)),
        ("agent:main:service:report-bot", None, None, ("user", None)),
        ("agent:main:shared", None, None, ("user", None)),
    ],
)
def test_a_conversation_is_shared_only_when_known_to_be(key, person, owner, expected):
    assert (
        segments.conversation_scope(
            key, person, owner_person_id=owner, main_session_key="agent:main:primary"
        )
        == expected
    )


def test_reader_principals_name_the_person_and_the_user():
    assert segments.reader_principals("p1", "u1") == ["person:p1", "user:u1"]
    assert segments.reader_principals(None, "u1") == ["user:u1"]
    assert segments.reader_principals("p1", None) == ["person:p1"]
    assert segments.reader_principals(None, None) == []
    assert segments.reader_principals("", "") == []


# ── backfill is bounded ────────────────────────────────────────────────────


async def test_one_run_indexes_at_most_the_turn_cap(monkeypatch):
    seen: list[int] = []

    def load(session_key, tenant_id, retention_days, max_turns):
        seen.append(max_turns)
        return

    monkeypatch.setattr(segments, "_load_conversation", load)
    await segments.index_conversation("agent:main:primary", tenant_id="test-tenant")
    assert seen == [segments.SEGMENT_INDEX_MAX_TURNS]
    assert segments.SEGMENT_INDEX_MAX_TURNS >= 2 * segments.SEGMENT_MAX_TURNS


async def test_a_second_run_for_the_same_conversation_is_skipped_while_one_is_running(
    monkeypatch,
):
    import asyncio

    calls = 0
    release = asyncio.Event()

    def load(*_a, **_kw):
        nonlocal calls
        calls += 1
        return

    async def slow_load(*a, **kw):
        await release.wait()
        return load(*a, **kw)

    real_to_thread = asyncio.to_thread

    async def to_thread(fn, *a, **kw):
        if fn is segments._load_conversation:
            return await slow_load(*a, **kw)
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(segments, "_load_conversation", load)
    monkeypatch.setattr(segments.asyncio, "to_thread", to_thread)

    first = asyncio.create_task(
        segments.index_conversation("agent:main:primary", tenant_id="test-tenant")
    )
    await asyncio.sleep(0)
    second = await segments.index_conversation("agent:main:primary", tenant_id="test-tenant")
    other = asyncio.create_task(
        segments.index_conversation("agent:main:primary", tenant_id="other-tenant")
    )
    await asyncio.sleep(0)
    release.set()
    await first
    await other

    assert second == 0
    assert calls == 2  # the first run and the other tenant's, never the duplicate


# ── what the reranker reads ────────────────────────────────────────────────


def test_the_reranker_excerpt_is_bounded_and_centred_on_the_match():
    text = (
        ("Filler about the weather and nothing else. " * 60)
        + "The locker code is 4417. "
        + ("More filler about lunch plans. " * 60)
    )
    excerpt = segments.rerank_excerpt(text, "what is the locker code?")

    assert len(excerpt) <= segments.SEGMENT_RERANK_CHARS
    assert "locker code is 4417" in excerpt


def test_the_reranker_excerpt_is_the_head_without_a_match():
    text = "abc " * 1000
    excerpt = segments.rerank_excerpt(text, "nothing matches here")
    assert excerpt == text[: segments.SEGMENT_RERANK_CHARS]


def test_a_short_text_is_its_own_excerpt():
    assert segments.rerank_excerpt("User: hi", "hi") == "User: hi"


# ── the minimum ────────────────────────────────────────────────────────────


def test_a_trailing_acknowledgement_joins_the_previous_segment():
    ack = Turn(
        ref=5,
        role="user",
        text="thanks!",
        at=BASE + timedelta(seconds=segments.SEGMENT_GAP_SECONDS + 60),
        embedding=fake_embed("thanks!"),
    )
    segs = segment_turns([*turns_of(BIKE), ack])
    assert ranges(segs) == [(1, 5)]


def test_a_lone_acknowledgement_is_not_a_segment():
    assert segment_turns([Turn(ref=1, role="user", text="ok", at=BASE)]) == []


def test_only_user_and_assistant_turns_with_text_are_segmented():
    turns = [
        Turn(ref=1, role="system", text="You are a helpful agent.", at=BASE),
        *turns_of(BIKE, start_ref=2),
        Turn(ref=6, role="assistant", text="   ", at=BASE + timedelta(minutes=9)),
    ]
    segs = segment_turns(turns)
    assert [t.ref for s in segs for t in s.turns] == [2, 3, 4, 5]


# ── embeddings are injected, not fetched ───────────────────────────────────


def test_turns_without_vectors_are_embedded_by_the_injected_callable():
    seen: list[list[str]] = []

    def embed(texts: list[str]) -> list[list[float] | None]:
        seen.append(list(texts))
        return fake_embed_batch(texts)

    segs = segment_turns(turns_of(BIKE + DENTIST, embedded=False), embed=embed)
    assert ranges(segs) == [(1, 4), (5, 8)]
    assert len(seen) == 1 and len(seen[0]) == 8


def test_without_vectors_only_gaps_and_size_split():
    segs = segment_turns(turns_of(BIKE + DENTIST, embedded=False))
    assert ranges(segs) == [(1, 8)]


def test_segmentation_is_deterministic():
    turns = turns_of(BIKE + DENTIST + BIKE)
    assert ranges(segment_turns(turns)) == ranges(segment_turns(list(turns)))
    assert [s.text for s in segment_turns(turns)] == [s.text for s in segment_turns(turns)]


@pytest.mark.parametrize(
    "name",
    [
        "SEGMENT_GAP_SECONDS",
        "SEGMENT_SIMILARITY_BREAK",
        "SEGMENT_WINDOW_TURNS",
        "SEGMENT_MAX_CHARS",
        "SEGMENT_MAX_TURNS",
        "SEGMENT_MIN_TURNS",
        "SEGMENT_MIN_CHARS",
        "SEGMENT_RETENTION_DAYS",
    ],
)
def test_the_thresholds_are_module_constants(name):
    assert isinstance(getattr(segments, name), int | float)


def test_segments_live_no_longer_than_the_chat_they_copy():
    from robothor.engine.chat_store import CHAT_TURN_TTL_DAYS

    assert segments.SEGMENT_RETENTION_DAYS <= CHAT_TURN_TTL_DAYS


# ── search needs a tenant ──────────────────────────────────────────────────


async def test_search_segments_has_no_default_tenant():
    with pytest.raises(TypeError):
        await segments.search_segments("bike locker code")  # type: ignore[call-arg]


async def test_search_segments_refuses_an_empty_tenant():
    with pytest.raises(ValueError):
        await segments.search_segments("bike locker code", tenant_id="", limit=5)


async def test_index_conversation_refuses_an_empty_tenant():
    with pytest.raises(ValueError):
        await segments.index_conversation("agent:main:primary", tenant_id="")


# ── retention and tenancy wiring ───────────────────────────────────────────


async def test_the_nightly_chat_ttl_step_prunes_segments_too(monkeypatch):
    from robothor.memory import lifecycle

    calls: list[tuple[str, int, str]] = []

    def chat(days, tenant):
        calls.append(("chat", days, tenant))
        return 1

    def segs(days, tenant):
        calls.append(("segments", days, tenant))
        return 2

    monkeypatch.setattr("robothor.engine.chat_store.cleanup_stale_chat_turns", chat)
    monkeypatch.setattr("robothor.memory.segments.cleanup_stale_segments", segs)

    stats = await lifecycle._chat_ttl_step(["tenant-one", "tenant-two"])

    assert stats == {"chat_pruned": 2, "segments_pruned": 4}
    for tenant in ("tenant-one", "tenant-two"):
        assert ("segments", segments.SEGMENT_RETENTION_DAYS, tenant) in calls
        assert any(c[0] == "chat" and c[2] == tenant for c in calls)


async def test_a_segment_prune_failure_does_not_stop_the_chat_ttl(monkeypatch):
    from robothor.memory import lifecycle

    def segs(days, tenant):
        raise RuntimeError("no table")

    monkeypatch.setattr("robothor.engine.chat_store.cleanup_stale_chat_turns", lambda d, t: 3)
    monkeypatch.setattr("robothor.memory.segments.cleanup_stale_segments", segs)

    stats = await lifecycle._chat_ttl_step(["tenant-one"])

    assert stats == {"chat_pruned": 3, "segments_pruned": 0}


def test_segments_move_with_their_conversation_in_a_workload_move():
    from robothor.tenancy.move_workload import MOVED_TABLES, UNTOUCHED_TABLES

    assert "memory_segments" not in UNTOUCHED_TABLES
    assert "_mw_sessions" in MOVED_TABLES["memory_segments"]
