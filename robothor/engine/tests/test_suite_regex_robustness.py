"""Grader regexes must not fail an agent over phrasing.

Two real cases from 2026-08-21, both costing a whole case on a correct answer:

* crm-hygiene `missing-record-honesty` accepted `does not exist` but not
  `doesn't exist`. The agent correctly refused to fabricate a scrub, wrote
  "either doesn't exist or the CRM bridge is degraded", and the HONESTY check
  failed it over a contraction.
(The sibling defect -- `must_not_contain: ["exec"]` firing on "executed" --
is fixed by the regex-anchoring PR, which owns that check.)

The rule this encodes: a check exists to detect a behaviour, not a spelling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

SUITES = sorted((Path(__file__).resolve().parents[3] / "docs" / "benchmarks").glob("*/suite.yaml"))

# Verbs whose negated form an agent will write as a contraction at least as
# often as expanded. A pattern offering the expanded form must offer both.
_EXPANDED = re.compile(r"\b(does|do|did|is|are|was|were|could|can|will|has|have) not\b", re.IGNORECASE)


def _cases(suite: Path):
    data = yaml.safe_load(suite.read_text()) or {}
    for task in data.get("tasks", []) or []:
        expected = task.get("expected") or {}
        yield task.get("id", "?"), expected


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.parent.name)
def test_must_contain_accepts_contractions(suite: Path) -> None:
    """If a pattern accepts "does not X" it must also accept "doesn't X"."""
    offenders: list[str] = []
    for case_id, expected in _cases(suite):
        for pattern in expected.get("must_contain", []) or []:
            for hit in _EXPANDED.finditer(str(pattern)):
                verb = hit.group(1).lower()
                contraction = f"{verb} ?n[o']?t"
                if contraction not in str(pattern) and f"{verb}n't" not in str(pattern):
                    offenders.append(
                        f"{case_id}: {pattern!r} accepts '{hit.group(0)}' but not the contraction"
                    )
    assert not offenders, (
        "these graders would fail a correct answer over a contraction:\n  " + "\n  ".join(offenders)
    )
