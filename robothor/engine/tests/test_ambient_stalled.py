"""Stalled-thread detection, on fixed timestamps.

A thread is stalled when the last message from a person is older than
``stalled_after`` and nothing from us came after it. Everything else — a
recent inbound, an inbound we already answered, a thread whose last word is
ours — is not, however old it is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robothor.engine.ambient import Message, Thread, find_stalled

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
FOUR_HOURS = 4 * 3600


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 31, hour, minute, tzinfo=UTC)


def _thread(thread_id: str, *messages: tuple[str, datetime]) -> Thread:
    return Thread(
        source="inbox",
        thread_id=thread_id,
        person="Alice",
        messages=[Message(direction=d, at=at) for d, at in messages],
    )


def test_only_the_old_unanswered_inbound_is_stalled():
    threads = [
        _thread("old-unanswered", ("inbound", _at(7))),
        _thread("recent-inbound", ("inbound", _at(9))),
        _thread("answered", ("inbound", _at(7)), ("outbound", _at(8))),
    ]
    stalled = find_stalled(threads, FOUR_HOURS, NOW)
    assert [s.thread_id for s in stalled] == ["old-unanswered"]
    assert stalled[0].waited_seconds == 5 * 3600
    assert stalled[0].ref == "inbox:old-unanswered"


def test_a_new_inbound_after_our_reply_restarts_the_clock():
    threads = [
        _thread("follow-up", ("inbound", _at(5)), ("outbound", _at(6)), ("inbound", _at(7))),
        _thread(
            "follow-up-recent", ("inbound", _at(5)), ("outbound", _at(6)), ("inbound", _at(11))
        ),
    ]
    stalled = find_stalled(threads, FOUR_HOURS, NOW)
    assert [s.thread_id for s in stalled] == ["follow-up"]
    # The clock runs from the first unanswered inbound, not the last one.
    assert stalled[0].last_inbound_at == _at(7)


def test_the_clock_starts_at_the_first_unanswered_inbound():
    threads = [_thread("two-asks", ("inbound", _at(7)), ("inbound", _at(10)))]
    stalled = find_stalled(threads, FOUR_HOURS, NOW)
    assert [s.thread_id for s in stalled] == ["two-asks"]
    assert stalled[0].waited_seconds == 5 * 3600


def test_a_thread_whose_last_word_is_ours_is_never_stalled():
    threads = [_thread("ours", ("outbound", _at(1)))]
    assert find_stalled(threads, FOUR_HOURS, NOW) == []


def test_exactly_at_the_threshold_is_stalled():
    threads = [_thread("edge", ("inbound", NOW - timedelta(seconds=FOUR_HOURS)))]
    assert [s.thread_id for s in find_stalled(threads, FOUR_HOURS, NOW)] == ["edge"]


def test_messages_out_of_order_are_sorted_before_judging():
    threads = [_thread("shuffled", ("outbound", _at(8)), ("inbound", _at(7)))]
    assert find_stalled(threads, FOUR_HOURS, NOW) == []


def test_longest_wait_comes_first():
    threads = [
        _thread("b", ("inbound", _at(6))),
        _thread("a", ("inbound", _at(2))),
    ]
    assert [s.thread_id for s in find_stalled(threads, FOUR_HOURS, NOW)] == ["a", "b"]
