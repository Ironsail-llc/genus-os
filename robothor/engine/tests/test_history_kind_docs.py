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


# Strict pattern: only matches a literal `metadata={"kind": "<val>"}` —
# the canonical idiom for calls to ``_record_transition`` /
# ``append_task_history`` and the way every new history producer should
# spell it. We deliberately do NOT match bare `"kind": "..."` literals;
# that catches false positives from JSON-schema definitions, browser
# tool requests, session-goal evidence kinds, and other unrelated dict
# literals. New writers that build their metadata via ``json.dumps(...)``
# positionally instead of ``metadata=`` will not be auto-detected; the
# minimum-set test below still pins the required kinds.
_KIND_LITERAL_PATTERN = re.compile(r"""metadata\s*=\s*\{\s*["']kind["']\s*:\s*["']([a-z_]+)["']""")


def _history_writer_files() -> list[Path]:
    """Return the list of production source files that may write to history.

    Maintenance contract — when you add a new module that calls
    ``_record_transition`` / ``append_task_history`` outside the directories
    listed here, extend this function. The current scope:

      * ``robothor/crm/dal.py`` — the canonical writer (set_question,
        approve_task, reject_task, answer_question, append_task_history,
        and all transition records).
      * ``robothor/engine/todo_promotion.py`` — Phase 3's subtask
        promotion path (when present on this branch).
      * ``robothor/engine/tools/**/*.py`` — every engine tool. The doc
        names the email-responder tool as the producer of ``email_sent``
        and the calendar-ingest tool as the producer of
        ``calendar_offer_received``; widening the scope means a new tool
        module is picked up automatically without anyone having to
        remember to update this list.

    Tests are intentionally not in scope — test fixtures may mock history
    rows with kinds that originate from external producers, which is fine.
    """
    files: list[Path] = []
    canonical = [
        REPO_ROOT / "robothor" / "crm" / "dal.py",
        REPO_ROOT / "robothor" / "engine" / "todo_promotion.py",
    ]
    files.extend(p for p in canonical if p.exists())
    tools_dir = REPO_ROOT / "robothor" / "engine" / "tools"
    if tools_dir.exists():
        files.extend(sorted(tools_dir.rglob("*.py")))
    return files


def _scrape_code_kinds() -> set[str]:
    """Collect every `metadata={"kind": "<val>"}` literal from history-writing
    source files. The strict regex above suppresses false positives.
    """
    found: set[str] = set()
    for py in _history_writer_files():
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _KIND_LITERAL_PATTERN.finditer(text):
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


# ─── Regex behavior — proves the strict pattern catches the right thing ───
# These pin the tightening intent so a future "let's loosen it again" edit
# trips the test instead of silently re-introducing the false positives Philip
# flagged on the original PR review (concern #3).


def test_pattern_matches_metadata_kwarg_double_quoted():
    """The canonical idiom: metadata={"kind": "..."}."""
    snippet = 'metadata={"kind": "plan", "agent": "x"}'
    assert _KIND_LITERAL_PATTERN.findall(snippet) == ["plan"]


def test_pattern_matches_metadata_kwarg_single_quoted_and_spaced():
    """Whitespace and single quotes are tolerated."""
    snippet = "metadata = { 'kind' : 'todo_promoted' , 'hash': 'abc' }"
    assert _KIND_LITERAL_PATTERN.findall(snippet) == ["todo_promoted"]


def test_pattern_ignores_bare_kind_literal():
    """A standalone `{"kind": "..."}` outside metadata= is NOT history metadata.
    The old loose pattern matched this and pulled false positives from JSON
    schemas, browser tool requests, and session_goal evidence kinds."""
    snippet = '{"kind": "click", "selector": "#submit"}'  # browser tool arg
    assert _KIND_LITERAL_PATTERN.findall(snippet) == []


def test_pattern_ignores_json_dumps_positional_kind():
    """json.dumps({"kind": ...}) passed positionally is not auto-detected.
    The minimum-set test pins the required kinds; new producers using this
    idiom must extend either this regex or the doc explicitly."""
    snippet = 'cur.execute("INSERT ...", (id, json.dumps({"kind": "plan"})))'
    assert _KIND_LITERAL_PATTERN.findall(snippet) == []


def test_pattern_ignores_dict_with_unrelated_kind_key():
    """Session-goal evidence kinds (note/commit/test_run/ci_run) live under
    `evidence={"kind": "..."}` and are unrelated to history metadata. The
    strict regex's `metadata=` anchor keeps them out."""
    snippet = 'evidence={"kind": "test_run", "summary": "pytest passed"}'
    assert _KIND_LITERAL_PATTERN.findall(snippet) == []
