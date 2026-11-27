"""The slot judge on the real local model: no true fact is closed.

``conflicts.judge_slot_change`` decides whether a new value replaces an old
one. Only ``changed`` closes a fact (``conflicts.slot_action``), so the
property that matters is that no pair labelled as compatible (both can be
true) ends with a close once the judge's answer goes through the decision
table. Exact outputs are not asserted. Runs only under
``-m llm`` against a local model; prints the confusion table.
"""

from __future__ import annotations

from collections import Counter

import pytest

pytestmark = [pytest.mark.llm, pytest.mark.asyncio]

#: (old, new, attribute, several-valued, new event date, label). Labels:
#: "change" (the new value replaces the old), "keep" (both can be true).
CASES = [
    ("Alice's email is alice@example.com.", "Alice's email is alice@work.example.",
     "email", True, None, "keep"),
    ("Alice's phone number is 555-0100.", "Alice's phone number is 555-0199.",
     "phone", True, None, "keep"),
    ("Alice's title is CTO.", "Alice's title is CEO.", "title", False, None, "change"),
    ("Alice lives in Boston.", "Alice moved to Cambridge.", "residence", False, None, "change"),
    ("Alice drives a Tesla.", "Alice drives a Ford.", "car", False, None, "change"),
    ("Alice's manager is Bob.", "Alice's boss is Bob.", "manager", False, None, "keep"),
    ("Alice lives in Boston.", "Alice moved to Denver in 2019.", "residence", False,
     "2019-06-01", "keep"),
    ("Alice lives in Boston.", "Alice lives in Massachusetts.", "residence", False, None,
     "keep"),
    ("Alice lives in Boston.", "Alice lives in Back Bay.", "residence", False, None, "keep"),
    ("Alice lives in Boston.", "Alice is based in London.", "residence", False, None, "keep"),
]  # fmt: skip


@pytest.mark.timeout(600)
async def test_no_compatible_pair_is_judged_changed() -> None:
    from robothor.memory.conflicts import (
        compare_slots,
        fact_slot,
        judge_slot_change,
        replacement_wording,
        slot_action,
    )

    table: Counter[tuple[str, str, str]] = Counter()
    wrong: list[tuple[str, str, str]] = []
    for old, new, _attribute, _several, new_event, label in CASES:
        # As the product does: the judge is asked only about two values of
        # one slot; any other pair is never compared, so nothing can close.
        new_slot, old_slot = fact_slot({"fact_text": new}), fact_slot({"fact_text": old})
        if compare_slots(new_slot, old_slot) != "change" or new_slot is None:
            table[(label, "not asked", "keep")] += 1
            print(f"{label:6} {'not asked':11} {'keep':10} | {old} -> {new}")
            continue
        several = new_slot.cardinality == "several"
        verdict = await judge_slot_change(
            new,
            old,
            attribute=new_slot.attribute,
            several=several,
            new_event=new_event,
            old_event=None,
            new_recorded="2026-11-27",
            old_recorded="2026-11-20",
            new_origin="user_stated",
            old_origin="user_stated",
        )
        outcome = verdict.get("outcome")
        # What the product does with the answer: only "close" ends a fact.
        action = slot_action(
            outcome,
            new_slot.cardinality,
            replacement_wording=replacement_wording(new),
            order="earlier" if new_event else "later",
        )
        table[(label, outcome or "unavailable", action)] += 1
        print(f"{label:6} {outcome or 'unavailable':11} {action:10} | {old} -> {new}")
        if label == "keep" and action == "close":
            wrong.append((old, new, verdict.get("reasoning", "")))
    print("\nlabel   judge        action     count")
    for (label, outcome, action), n in sorted(table.items()):
        print(f"{label:7} {outcome:12} {action:10} {n}")
    assert not wrong, wrong
