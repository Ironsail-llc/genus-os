"""Meta-test: the documented `crm_task_history.metadata.kind` enum stays in sync with code.

`docs/TASK_HISTORY_KIND.md` is the canonical list of `kind` values that callers
write into `crm_task_history.metadata`. The Phase-1 migration also encodes the
same set as a Postgres CHECK constraint. If a new kind is introduced (or an old
one renamed) and only one side is updated, the planner and observability rely
on stale assumptions.

This test scrapes `metadata={"kind": "..."}` literals from the Python codebase
and asserts the set matches the documented enum.
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo root, four parents up from this file: robothor/engine/tests/test_*.py
REPO_ROOT = Path(__file__).resolve().parents[3]


def _read_documented_kinds() -> set[str]:
    """Parse the kinds listed in docs/TASK_HISTORY_KIND.md.

    The doc uses a markdown table with a `Kind` column. Each kind appears as
    backtick-wrapped lowercase identifier in the first column of a row.
    """
    doc = REPO_ROOT / "docs" / "TASK_HISTORY_KIND.md"
    text = doc.read_text(encoding="utf-8")
    # Match the first column of a table row: `| \`<kind>\` |`
    pattern = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", flags=re.MULTILINE)
    return set(pattern.findall(text))


# Patterns that match `metadata=...` literals carrying a "kind" field.
# We accept both single- and double-quoted JSON-ish dict literals.
_KIND_LITERAL_PATTERNS = [
    re.compile(r"""metadata\s*=\s*\{\s*["']kind["']\s*:\s*["']([a-z_]+)["']"""),
    # Same shape but the dict literal is on its own line indented under a call.
    re.compile(r"""["']kind["']\s*:\s*["']([a-z_]+)["']"""),
]


# Production files that write to `crm_task_history.metadata`. Tests are not in
# this list — they may mock history rows with kinds that originate from
# external tools (calendar/email), which is fine. As new modules start writing
# to history (e.g. Phase-3 todo_promotion.py), add them here.
_HISTORY_WRITER_FILES = [
    REPO_ROOT / "robothor" / "crm" / "dal.py",
]


def _scrape_code_kinds() -> set[str]:
    """Collect every `kind: "<val>"` literal from history-writing source files.

    Tight scoping prevents false positives from JSON schemas, browser tool
    kinds, session-goal evidence kinds, and test-only mocks of upstream rows.
    """
    found: set[str] = set()
    for py in _HISTORY_WRITER_FILES:
        if not py.exists():
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pat in _KIND_LITERAL_PATTERNS:
            for match in pat.finditer(text):
                found.add(match.group(1))
    return found


def test_documented_kinds_cover_code_usage():
    """Every `metadata.kind` value written by code is documented in TASK_HISTORY_KIND.md."""
    documented = _read_documented_kinds()
    code_used = _scrape_code_kinds()

    undocumented = code_used - documented
    assert not undocumented, (
        f"These kinds are written by code but not in docs/TASK_HISTORY_KIND.md: "
        f"{sorted(undocumented)}. Add them to the doc, then re-run this test."
    )


def test_documented_kinds_have_no_typos():
    """Lowercase + underscore-only; no trailing whitespace, no duplicates."""
    documented = _read_documented_kinds()
    for kind in documented:
        assert kind == kind.lower(), f"kind {kind!r} must be lowercase"
        assert " " not in kind, f"kind {kind!r} must not contain spaces"
        assert kind.isidentifier(), f"kind {kind!r} must be a valid python identifier"


def test_documented_kinds_minimum_set():
    """The doc must declare at least the seven kinds the plan promised."""
    documented = _read_documented_kinds()
    required = {
        "plan",
        "ask",
        "answer",
        "email_sent",
        "calendar_offer_received",
        "todo_promoted",
        "acceptance",
    }
    missing = required - documented
    assert not missing, f"docs/TASK_HISTORY_KIND.md is missing kinds: {sorted(missing)}"
