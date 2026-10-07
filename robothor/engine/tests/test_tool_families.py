"""Every Workspace guard table derives from one set of tool families.

Safety and accounting code used to key on literal ``gws_*`` names copied into
fifteen modules. A later change routes the same ``gws_*`` tools to a second
mail/calendar provider, and every guard must keep applying to them — which only
holds if there is one canonical list. These tests pin three things:

* the families describe exactly the tools the gws handler module registers;
* the families relate the way the guards assume (a send is a write, ...);
* no module outside the canonical ones spells a Workspace tool name as a
  string literal again (the ratchet), and the rewired tables still hold the
  same names they held when they were literals.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from robothor.engine.tools import constants as c

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "robothor"

# The modules allowed to spell a Workspace tool name: the families themselves,
# the tool declarations the model sees, the search vocabulary, and the handler
# implementations.
CANONICAL = {
    "robothor/engine/tools/constants.py",
    "robothor/engine/tools/schemas.py",
    "robothor/engine/tools/keywords.py",
    "robothor/engine/tools/handlers/gws.py",
}

#: {path: reason}. Empty on purpose: add an entry only when a literal truly
#: cannot come from a family, and say why.
ALLOWLIST: dict[str, str] = {}

# A tool name, or a name prefix, as the WHOLE literal — or quoted inside SQL.
_NAME = re.compile(r"^gws_(gmail|calendar|chat)_")
_QUOTED = re.compile(r"'gws_(gmail|calendar|chat)_")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _stray_literals() -> list[str]:
    hits: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "tests" in path.relative_to(PACKAGE).parts or rel in CANONICAL or rel in ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        docs = _docstring_ids(tree)
        hits.extend(
            f"{rel}:{node.lineno} {node.value[:60]!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and (_NAME.match(node.value) or _QUOTED.search(node.value))
        )
    return hits


def test_no_module_spells_a_workspace_tool_name_outside_the_families() -> None:
    hits = _stray_literals()
    assert not hits, (
        "Workspace tool names must come from the families in "
        "robothor/engine/tools/constants.py, not literals:\n  " + "\n  ".join(hits)
    )


def test_allowlist_entries_still_exist() -> None:
    for rel in ALLOWLIST:
        assert (ROOT / rel).is_file(), f"stale allowlist entry: {rel}"


def test_workspace_tools_are_exactly_the_registered_gws_handlers() -> None:
    from robothor.engine.tools.handlers import gws

    assert set(gws.HANDLERS) == c.WORKSPACE_TOOLS
    assert c.GWS_TOOLS == c.WORKSPACE_TOOLS


def test_family_relationships() -> None:
    assert c.MAIL_SEND_TOOLS < c.MAIL_WRITE_TOOLS
    assert c.CALENDAR_CREATE_TOOLS < c.CALENDAR_WRITE_TOOLS
    assert c.CALENDAR_EDIT_TOOLS < c.CALENDAR_WRITE_TOOLS
    assert not c.CALENDAR_CREATE_TOOLS & c.CALENDAR_EDIT_TOOLS
    assert c.CHAT_READ_TOOLS | c.CHAT_SEND_TOOLS == c.CHAT_TOOLS
    reads = c.MAIL_READ_TOOLS | c.CALENDAR_READ_TOOLS | c.CHAT_READ_TOOLS
    writes = c.MAIL_WRITE_TOOLS | c.CALENDAR_WRITE_TOOLS | c.CHAT_SEND_TOOLS
    assert not reads & writes
    assert reads | writes == c.WORKSPACE_TOOLS
    # The mail family is one provider's surface, not every tool with a name.
    assert all(name.startswith("gws_gmail_") for name in c.MAIL_READ_TOOLS | c.MAIL_WRITE_TOOLS)
    assert all(
        name.startswith("gws_calendar_") for name in c.CALENDAR_READ_TOOLS | c.CALENDAR_WRITE_TOOLS
    )
    assert all(name.startswith("gws_chat_") for name in c.CHAT_TOOLS)


# ── The rewired tables still hold the names they held as literals ─────────

_SEND = {"gws_gmail_send", "gws_gmail_reply"}
_CAL_WRITES = {
    "gws_calendar_create",
    "gws_calendar_update",
    "gws_calendar_add_attendees",
    "gws_calendar_respond",
    "gws_calendar_delete",
}
_ALL = {
    "gws_gmail_search",
    "gws_gmail_get",
    "gws_gmail_reply",
    "gws_gmail_send",
    "gws_gmail_modify",
    "gws_calendar_list",
    *_CAL_WRITES,
    "gws_chat_send",
    "gws_chat_list_spaces",
    "gws_chat_list_messages",
}


def test_families_hold_the_names_they_replaced() -> None:
    assert c.WORKSPACE_TOOLS == _ALL
    assert {"gws_gmail_search", "gws_gmail_get"} == c.MAIL_READ_TOOLS
    assert c.MAIL_SEND_TOOLS == _SEND
    assert _SEND | {"gws_gmail_modify"} == c.MAIL_WRITE_TOOLS
    assert {"gws_calendar_list"} == c.CALENDAR_READ_TOOLS
    assert {"gws_calendar_create"} == c.CALENDAR_CREATE_TOOLS
    assert {
        "gws_calendar_update",
        "gws_calendar_add_attendees",
        "gws_calendar_respond",
    } == c.CALENDAR_EDIT_TOOLS
    assert c.CALENDAR_WRITE_TOOLS == _CAL_WRITES
    assert {"gws_chat_send", "gws_chat_list_spaces", "gws_chat_list_messages"} == c.CHAT_TOOLS


def _attr(module: str, name: str) -> object:
    return getattr(importlib.import_module(module), name)


_READS = {
    "gws_gmail_search",
    "gws_gmail_get",
    "gws_calendar_list",
    "gws_chat_list_spaces",
    "gws_chat_list_messages",
}


@pytest.mark.parametrize(
    ("module", "name", "expected"),
    [
        ("robothor.engine.tools.constants", "READONLY_TOOLS", _READS),
        ("robothor.engine.tools.constants", "CORE_TOOLS", _CAL_WRITES | {"gws_calendar_list"}),
        ("robothor.engine.guardrails", "_EMAIL_SEND_TOOLS", _SEND),
        ("robothor.goals.evidence", "CALENDAR_EFFECT_TOOLS", _CAL_WRITES),
        ("robothor.engine.recent_actions", "_NOTABLE_TOOLS", _CAL_WRITES | _SEND),
        (
            "robothor.engine.tools.verification",
            "POST_CONDITION_CHECKS",
            _SEND | {"gws_calendar_create"},
        ),
        ("robothor.engine.benchmark_sandbox", "EXTERNAL_SIDE_EFFECT_TOOLS", _ALL),
        (
            "robothor.engine.tools.handlers.gws",
            "_GWS_MUTATING_TOOLS",
            _CAL_WRITES | _SEND | {"gws_gmail_modify", "gws_chat_send"},
        ),
        (
            "robothor.engine.tools.handlers.gws",
            "_CALENDAR_EDITS",
            {"gws_calendar_update", "gws_calendar_add_attendees", "gws_calendar_respond"},
        ),
    ],
)
def test_rewired_table_holds_the_same_workspace_tools(module, name, expected) -> None:
    assert set(_attr(module, name)) & _ALL == expected


def test_benchmark_deny_list_still_holds_the_chat_send() -> None:
    from robothor.engine.tools.handlers.benchmark import benchmark_readonly_tools

    assert "gws_chat_send" not in benchmark_readonly_tools()
    assert not benchmark_readonly_tools() & _ALL


@pytest.mark.parametrize(
    ("tool", "family"),
    [
        ("gws_gmail_send", "email_send"),
        ("gws_gmail_reply", "email_send"),
        ("gws_chat_send", "message_send"),
        *[(name, "calendar_write") for name in sorted(_CAL_WRITES)],
    ],
)
def test_run_verification_still_classifies_workspace_writes(tool, family) -> None:
    from robothor.engine.run_verification import _tool_families

    assert family in _tool_families(tool, {})


def test_run_verification_does_not_count_workspace_reads_as_writes() -> None:
    from robothor.engine.run_verification import _tool_families

    for name in _READS | {"gws_gmail_modify"}:
        assert not _tool_families(name, {}), name


def test_post_condition_checkers_are_unchanged() -> None:
    from robothor.engine.tools import verification as v

    checks = v.POST_CONDITION_CHECKS
    assert checks["gws_gmail_send"] is v._check_gmail_message
    assert checks["gws_gmail_reply"] is v._check_gmail_message
    assert checks["gws_calendar_create"] is v._check_calendar_event


@pytest.mark.parametrize(
    "module",
    [
        "robothor.engine.run_verification",
        "robothor.engine.guardrails",
        "robothor.engine.recent_actions",
        "robothor.engine.chat_receipts",
        "robothor.engine.performance",
        "robothor.goals.evidence",
        "robothor.doctor.checks.tools",
        "robothor.engine.tools.verification",
    ],
)
def test_module_imports_first_without_a_cycle(module: str) -> None:
    """The families live inside the tools package, which imports half the
    engine. A module that imports them at the top and is itself imported by
    the tools package is a cycle that only shows when it is loaded first."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
