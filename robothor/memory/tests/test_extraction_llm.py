"""The one extraction against a real local model (``llm`` marker only).

Ten generic passages — conversation turns, email-like and CRM-note-like text —
each with what a correct extraction must contain: the names that must be
mentioned with their type, the relations that must hold between stated names,
and the event date (or ``None`` when the passage states none). Run with ``-s``
to see the per-passage report:

    pytest robothor/memory/tests/test_extraction_llm.py -m llm -s

The assertion is only that the structured output is valid every time (the
schema is enforced by constrained decoding, so anything else is a bug). The
quality numbers are reported, not gated: they are evidence for whether a small
model can carry the combined structure, measured honestly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.llm


@dataclass
class Case:
    kind: str
    text: str
    mentions: dict[str, str]
    relations: list[tuple[str, str]] = field(default_factory=list)  # (source, target)
    event_time: str | None = None


CASES = [
    Case(
        "conversation",
        "User: I started at Northwind Traders on 2026-02-02 as a data engineer.",
        {"Northwind Traders": "organization"},
        [],
        "2026-02-02",
    ),
    Case(
        "conversation",
        "User: Bob Chen is my manager now, and he wants Project Helios done in Python.",
        {"Bob Chen": "person", "Project Helios": "project", "Python": "technology"},
        [("Project Helios", "Python")],
        None,
    ),
    Case(
        "conversation",
        "Assistant: Noted — you prefer morning meetings and you live in Lisbon.",
        {"Lisbon": "location"},
        [],
        None,
    ),
    Case(
        "email",
        "From: news@example.com\nSubject: Leadership update\n\nGlobex Corporation "
        "announced on 2026-03-10 that Carol Diaz will become its chief financial officer.",
        {"Globex Corporation": "organization", "Carol Diaz": "person"},
        [("Carol Diaz", "Globex Corporation")],
        "2026-03-10",
    ),
    Case(
        "email",
        "Hi Alice,\nThe Initech Systems renewal is signed. Dana Reyes will run the "
        "onboarding in Berlin next month.\nThanks, Erin",
        {"Initech Systems": "organization", "Dana Reyes": "person", "Berlin": "location"},
        [],
        None,
    ),
    Case(
        "email",
        "Reminder: the Atlas Summit takes place in Toronto on 2026-06-18. Frank Osei "
        "from Umbrella Labs is the keynote speaker.",
        {
            "Atlas Summit": "event",
            "Toronto": "location",
            "Frank Osei": "person",
            "Umbrella Labs": "organization",
        },  # fmt: skip
        [("Frank Osei", "Umbrella Labs")],
        "2026-06-18",
    ),
    Case(
        "crm_note",
        "Call with Grace Kim (VP Sales, Hooli). She wants a pilot of Project Orion "
        "on Kubernetes. Follow up after their budget review.",
        {
            "Grace Kim": "person",
            "Hooli": "organization",
            "Project Orion": "project",
            "Kubernetes": "technology",
        },  # fmt: skip
        [("Grace Kim", "Hooli")],
        None,
    ),
    Case(
        "crm_note",
        "Meeting 2026-04-07: Henry Park of Vandelay Industries agreed to migrate "
        "billing to PostgreSQL by the end of Q3.",
        {
            "Henry Park": "person",
            "Vandelay Industries": "organization",
            "PostgreSQL": "technology",
        },  # fmt: skip
        [("Henry Park", "Vandelay Industries")],
        "2026-04-07",
    ),
    Case(
        "crm_note",
        "Iris Novak moved from Acme Robotics to Stark Analytics. Update her contact record.",
        {
            "Iris Novak": "person",
            "Acme Robotics": "organization",
            "Stark Analytics": "organization",
        },  # fmt: skip
        [("Iris Novak", "Stark Analytics")],
        None,
    ),
    Case(
        "conversation",
        "User: Jonas Weber from Wayne Logistics called; they chose Grafana for "
        "monitoring the Delta warehouse project.",
        {"Jonas Weber": "person", "Wayne Logistics": "organization", "Grafana": "technology"},
        [("Jonas Weber", "Wayne Logistics")],
        None,
    ),
]


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


@pytest.mark.asyncio
@pytest.mark.timeout(1800)
async def test_the_one_extraction_on_a_real_model() -> None:
    from robothor.memory.facts import extract_facts_detailed

    totals = {
        "valid": 0,
        "mentions_expected": 0,
        "mentions_found": 0,
        "mentions_typed_right": 0,
        "relations": 0,
        "relations_grounded": 0,
        "relations_expected": 0,
        "relations_found": 0,
        "event_right": 0,
        "facts": 0,
    }
    seconds: list[float] = []
    for i, case in enumerate(CASES, 1):
        started = time.monotonic()
        out = await extract_facts_detailed(case.text, max_retries=1, timeout=180)
        seconds.append(time.monotonic() - started)
        if out.error is None and out.facts:
            totals["valid"] += 1
        totals["facts"] += len(out.facts)
        found: dict[str, str | None] = {}
        rels: list[tuple[str, str]] = []
        times: set[str | None] = set()
        for f in out.facts:
            names = {_norm(m["surface_form"]) for m in f["mentions"]}
            for m in f["mentions"]:
                found.setdefault(_norm(m["surface_form"]), m["entity_type"])
            for r in f["relations"]:
                totals["relations"] += 1
                grounded = _norm(r["source"]) in names and _norm(r["target"]) in names
                totals["relations_grounded"] += int(grounded)
                rels.append((_norm(r["source"]), _norm(r["target"])))
            times.add((f["event_time"] or "")[:10] or None)
        for name, etype in case.mentions.items():
            totals["mentions_expected"] += 1
            if _norm(name) in found:
                totals["mentions_found"] += 1
                totals["mentions_typed_right"] += int(found[_norm(name)] == etype)
        for s, t in case.relations:
            totals["relations_expected"] += 1
            pair = {_norm(s), _norm(t)}
            totals["relations_found"] += int(any({a, b} == pair for a, b in rels))
        right = (case.event_time in times) if case.event_time else (times <= {None})
        totals["event_right"] += int(right)
        print(
            f"[{i:2d} {case.kind:12s}] {seconds[-1]:5.1f}s error={out.error} facts={len(out.facts)} "
            f"mentions={sorted(found.items())} relations={rels} event={sorted(map(str, times))} "
            f"event_ok={right}"
        )
    n = len(CASES)
    print(
        f"\nvalid structured output: {totals['valid']}/{n}\n"
        f"expected mentions found: {totals['mentions_found']}/{totals['mentions_expected']}, "
        f"typed right: {totals['mentions_typed_right']}/{totals['mentions_found']}\n"
        f"relations grounded in the fact's mentions: "
        f"{totals['relations_grounded']}/{totals['relations']}\n"
        f"expected relations found: {totals['relations_found']}/{totals['relations_expected']}\n"
        f"event time right or correctly null: {totals['event_right']}/{n}\n"
        f"seconds per passage: median {sorted(seconds)[n // 2]:.1f}, max {max(seconds):.1f}"
    )
    assert totals["valid"] == n
