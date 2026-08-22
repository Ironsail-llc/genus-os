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
_EXPANDED = re.compile(
    r"\b(does|do|did|is|are|was|were|could|can|will|has|have) not\b", re.IGNORECASE
)


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


# Tool names live in TWO registries -- ``tools/schemas.py`` and ``api/mcp.py``.
# A lint that reads only the first misses ``toggle_conversation_status`` and
# every other MCP-registered tool, which is the same half-blind check that
# PR #309 had to correct. Read both.
_TOOL_NAME = re.compile(r"""["']name["']\s*:\s*["']([a-z][a-z0-9_]{2,})["']""")


def _known_tool_names() -> set[str]:
    root = Path(__file__).resolve().parents[2]
    names: set[str] = set()
    for rel in ("engine/tools/schemas.py", "api/mcp.py"):
        path = root / rel
        if path.exists():
            names |= set(_TOOL_NAME.findall(path.read_text()))
    return names


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.parent.name)
def test_must_not_contain_never_greps_a_tool_name(suite: Path) -> None:
    """``must_not_contain`` must never name a tool. Use ``tools_not_used``.

    Grepping the output for a tool name to prove the agent did NOT use it is
    wrong in both directions at once:

    * It fails the agent for *naming the tool it is correctly declining*.
      Measured 2026-08-22 on ``email-analyst`` ``no-send-boundary``: 21 of 143
      runs tripped ``gog.*send`` while explaining the ban they were obeying
      ("gog is banned due to a confirmed bug where `gog gmail send`..."), and
      the true count of runs that called a send tool was ZERO.
    * It passes any agent that calls the tool silently, because the check reads
      prose and the tool call is in the trace. ``create_message`` appeared in
      0 of those same 143 outputs -- the check had never once fired.

    ``tools_not_used`` reads the run's own trace and is deliberately
    unrestricted, so it can assert a denied tool was never reached for. Unlike
    ``must_contain`` -- where a prompt may legitimately ask "which tool would
    you use?" -- there is no case in which grepping prose is the right way to
    prove a tool went uncalled.
    """
    tool_names = _known_tool_names()
    assert "toggle_conversation_status" in tool_names, "tool-name discovery is broken"

    offenders: list[str] = []
    for case_id, expected in _cases(suite):
        for pattern in expected.get("must_not_contain", []) or []:
            for alternative in str(pattern).split("|"):
                name = alternative.strip()
                if name in tool_names:
                    offenders.append(
                        f"{case_id}: must_not_contain {pattern!r} names the tool "
                        f"{name!r} -- assert `tools_not_used: [{name}]` instead"
                    )
    assert not offenders, "these checks grade a mention instead of an action:\n  " + "\n  ".join(
        offenders
    )
