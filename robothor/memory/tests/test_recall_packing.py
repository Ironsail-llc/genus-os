"""Packing cuts a row to the room left instead of leaving the room empty.

The packing rule skipped a row that did not fit and never cut one, so a
2,100-character segment followed by 2,000-character segments packed to 2,100
of 4,000 characters. Now, once every row that fits whole is in, the
best-ranked row that was skipped is cut to the room that is left and kept in
its rank position, marked like the first-row cut, when at least
``RECALL_MIN_CUT_CHARS`` remain. A context only gains text: every row the old
rule kept is still kept, whole. The recall tool, the routed path and the eval
share the rule.
"""

from __future__ import annotations

from typing import Any

from robothor.memory import eval_systems, recall
from robothor.memory.recall import RECALL_CONTEXT_CHARS, TRUNCATION_MARKER, within_context_cap

#: ``recall.RECALL_MIN_CUT_CHARS``, pinned by the first test.
MIN_CUT = 300


def _row(i: int, chars: int) -> dict[str, Any]:
    return {"id": i, "kind": "segment", "fact_text": "x" * chars}


def _used(rows: list[dict[str, Any]]) -> int:
    return sum(len(r["fact_text"]) for r in rows)


def test_the_useful_minimum_is_three_hundred_characters() -> None:
    assert recall.RECALL_MIN_CUT_CHARS == MIN_CUT


def test_a_row_that_does_not_fit_is_cut_to_the_room_left() -> None:
    rows = [_row(10, 2100), *[_row(11 + i, 2000) for i in range(5)]]
    kept = within_context_cap(rows)

    assert [r["id"] for r in kept] == [10, 11]
    assert kept[0]["fact_text"] == "x" * 2100
    assert kept[1]["fact_text"].endswith(TRUNCATION_MARKER)
    assert kept[1]["truncated"] is True
    assert _used(kept) == RECALL_CONTEXT_CHARS
    assert rows[1]["fact_text"] == "x" * 2000, "the input is untouched"


def test_rows_that_fit_whole_are_all_kept_and_the_cut_row_keeps_its_rank() -> None:
    rows = [_row(1, 100), _row(2, 3950), *[_row(i, 100) for i in range(3, 33)]]
    kept = within_context_cap(rows)

    ids = [r["id"] for r in kept]
    assert ids == [1, 2, *range(3, 33)], "every whole row stays; the cut one at rank 2"
    assert kept[1]["fact_text"].endswith(TRUNCATION_MARKER)
    assert all(not r["fact_text"].endswith(TRUNCATION_MARKER) for r in kept if r["id"] != 2)
    assert _used(kept) == RECALL_CONTEXT_CHARS


def test_less_than_the_minimum_left_is_left_empty() -> None:
    left = MIN_CUT - 1
    rows = [_row(1, RECALL_CONTEXT_CHARS - left), _row(2, 2000)]
    kept = within_context_cap(rows)

    assert [r["id"] for r in kept] == [1]


def test_exactly_the_minimum_left_is_used() -> None:
    rows = [_row(1, RECALL_CONTEXT_CHARS - MIN_CUT), _row(2, 2000)]
    kept = within_context_cap(rows)

    assert [r["id"] for r in kept] == [1, 2]
    assert len(kept[1]["fact_text"]) == MIN_CUT


def test_only_one_row_is_cut() -> None:
    rows = [_row(1, 1000), _row(2, 3500), _row(3, 3500)]
    kept = within_context_cap(rows)

    assert [r["id"] for r in kept] == [1, 2]
    assert _used(kept) == RECALL_CONTEXT_CHARS


def test_without_a_truncate_function_nothing_is_cut() -> None:
    items = ["a" * 600, "b" * 600]
    assert recall.pack_within(items, 1000, len) == ["a" * 600]


def test_the_eval_cuts_with_the_same_rule_in_tokens() -> None:
    """The eval sizes in tokens (chars / 4): the minimum is the same 300
    characters, 75 tokens."""
    min_tokens = MIN_CUT // 4
    cands = [("a", "turn", "a" * 4 * 500), ("b", "turn", "b" * 4 * 600)]
    items = eval_systems.fill_budget(cands, 500 + min_tokens)

    assert [i.id for i in items] == ["a", "b"]
    assert items[1].text.endswith(TRUNCATION_MARKER)
    assert sum(i.tokens for i in items) <= 500 + min_tokens

    short = eval_systems.fill_budget(cands, 500 + min_tokens - 1)
    assert [i.id for i in short] == ["a"]
