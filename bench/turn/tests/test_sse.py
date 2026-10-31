"""The webchat SSE stream becomes per-turn timings."""

from __future__ import annotations

import pytest

from bench.turn.sse import parse_sse, turn_timings

T0 = 100.0


def _stream() -> list[tuple[float, str]]:
    """(arrival time in seconds, line) pairs, as the HTTP half reads them."""
    return [
        (100.010, ": keepalive"),
        (100.010, ""),
        (100.050, "event: interim"),
        (100.050, 'data: {"text": "thinking"}'),
        (100.050, ""),
        (100.200, "event: delta"),
        (100.200, 'data: {"text": "po"}'),
        (100.200, ""),
        (100.250, "event: delta"),
        (100.250, 'data: {"text": "n"}'),
        (100.250, ""),
        (100.300, "event: delta"),
        (100.300, 'data: {"text": "g"}'),
        (100.300, ""),
        (100.420, "event: done"),
        (100.420, 'data: {"text": "pong", "status": "completed", "run_id": "run-1"}'),
        (100.420, ""),
    ]


def test_events_are_parsed_in_order_and_comments_are_ignored() -> None:
    events = parse_sse(_stream())
    assert [e.event for e in events] == ["interim", "delta", "delta", "delta", "done"]
    assert events[1].data == {"text": "po"}
    assert events[1].t == pytest.approx(100.200)


def test_a_multi_line_data_field_is_joined() -> None:
    events = parse_sse([(1.0, "event: done"), (1.0, 'data: {"a":'), (1.0, "data: 1}"), (1.0, "")])
    assert events[0].data == {"a": 1}


def test_a_trailing_event_without_a_blank_line_is_kept() -> None:
    events = parse_sse([(1.0, "event: done"), (1.0, 'data: {"run_id": "r"}')])
    assert [e.event for e in events] == ["done"]


def test_turn_timings_from_first_delta_last_delta_and_done() -> None:
    timing = turn_timings(parse_sse(_stream()), T0)
    assert timing.ttft_ms == pytest.approx(200.0)
    assert timing.done_ms == pytest.approx(420.0)
    assert timing.finalize_tail_ms == pytest.approx(120.0)
    assert timing.run_id == "run-1"
    assert timing.status == "completed"
    assert timing.error is None


def test_a_turn_with_no_delta_has_no_ttft_and_no_tail() -> None:
    events = parse_sse([(100.5, "event: done"), (100.5, 'data: {"run_id": "r2"}'), (100.5, "")])
    timing = turn_timings(events, T0)
    assert timing.ttft_ms is None
    assert timing.finalize_tail_ms is None
    assert timing.done_ms == pytest.approx(500.0)


def test_an_error_event_is_reported_and_has_no_done() -> None:
    events = parse_sse([(100.1, "event: error"), (100.1, 'data: {"error": "boom"}'), (100.1, "")])
    timing = turn_timings(events, T0)
    assert timing.error == "boom"
    assert timing.done_ms is None
