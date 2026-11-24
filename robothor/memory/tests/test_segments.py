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
