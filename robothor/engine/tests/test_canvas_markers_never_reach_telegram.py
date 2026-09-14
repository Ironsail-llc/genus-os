"""Canvas markers are metadata for the Helm, never text for a chat.

Live, 2026-09-14: the operator's Telegram reply from main ended in a raw
``[DASHBOARD:{"intent":"memory-health","data":{...}}]`` blob. The Helm
injects a "visual canvas" instruction into the owner's ONE shared session
(webchat and Telegram share it on purpose), so the model emits the marker
on every surface, and only the Helm knows how to swallow it. The splitter is
the one function every Telegram send and every delivery-count prediction
goes through, so it is where the marker leaves the text.
"""

from __future__ import annotations

from robothor.engine.chunking import split_message, split_telegram_message, strip_canvas_markers

_DASH = (
    '[DASHBOARD:{"intent":"memory-health","data":{"current":[{"block":"a","age":"2m"}],'
    '"stale":[{"block":"persona","issue":"claims [x] and {y}"}]}}]'
)


def test_a_trailing_dashboard_marker_is_removed() -> None:
    text = f"Want me to refresh persona now?\n\n{_DASH}"
    assert strip_canvas_markers(text) == "Want me to refresh persona now?"


def test_a_leading_dashboard_marker_is_removed() -> None:
    text = f"{_DASH}\nHere is the picture."
    assert strip_canvas_markers(text) == "Here is the picture."


def test_nested_braces_and_brackets_inside_the_json_do_not_end_the_marker_early() -> None:
    # The JSON above carries "[x]" and "{y}" inside strings and nested objects.
    assert "DASHBOARD" not in strip_canvas_markers(f"before {_DASH} after")
    assert strip_canvas_markers(f"before {_DASH} after") == "before  after".replace("  ", " ")


def test_render_markers_are_removed_too() -> None:
    text = 'Done.\n[RENDER:metric-grid:{"items":[{"t":"Healthy","v":3}]}]\nAnything else?'
    assert strip_canvas_markers(text) == "Done.\nAnything else?"


def test_ordinary_brackets_are_left_alone() -> None:
    text = "See [the docs](https://example.com) and [engine] context; array[0] = {a: 1}."
    assert strip_canvas_markers(text) == text


def test_an_unterminated_marker_is_dropped_to_the_end_rather_than_shown() -> None:
    # A truncated stream can cut the JSON; half a marker is still not for humans.
    text = 'All good.\n[DASHBOARD:{"intent":"x","data":{"a":[1,2'
    assert strip_canvas_markers(text) == "All good."


def test_the_splitter_strips_before_it_splits_so_both_counts_agree() -> None:
    body = "x" * 4000 + "\n" + _DASH
    assert split_telegram_message(body) == ["x" * 4000]
    assert split_message(body) == ["x" * 4000]


def test_a_message_that_is_only_a_marker_becomes_nothing_to_send() -> None:
    assert split_telegram_message(_DASH) == []
