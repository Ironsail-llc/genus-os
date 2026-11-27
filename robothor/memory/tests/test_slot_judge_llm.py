"""The slot judge on the real local model: no true fact is closed.

``conflicts.judge_slot_change`` decides whether a new value replaces an old
one. Only ``changed`` closes a fact (``conflicts.slot_action``), so the
property that matters is that no pair labelled as compatible (both can be
true) is judged ``changed``. Exact outputs are not asserted. Runs only under
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
    from robothor.memory.conflicts import judge_slot_change

    table: Counter[tuple[str, str]] = Counter()
    wrong: list[tuple[str, str, str]] = []
    for old, new, attribute, several, new_event, label in CASES:
        verdict = await judge_slot_change(
            new,
            old,
            attribute=attribute,
            several=several,
            new_event=new_event,
            old_event=None,
            new_recorded="2026-11-27",
            old_recorded="2026-11-20",
            new_origin="user_stated",
            old_origin="user_stated",
        )
        outcome = verdict.get("outcome") or "unavailable"
        table[(label, outcome)] += 1
        if label == "keep" and outcome == "changed":
            wrong.append((old, new, verdict.get("reasoning", "")))
    print("\nlabel   outcome      count")
    for (label, outcome), n in sorted(table.items()):
        print(f"{label:7} {outcome:12} {n}")
    assert not wrong, wrong
