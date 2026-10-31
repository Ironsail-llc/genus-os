"""No `except Exception: pass` outside tests.

A broad handler whose whole body is `pass` makes a failure and a healthy run
look the same from outside: nothing in the log, nothing in /health. Ruff's
S110 stops new ones per file, but it can be switched off per file; this gate
reads every tracked Python file outside the test trees and is the one place
the remaining exceptions are written down, each with its reason.

A handler passes the gate when it either catches a narrower type than
`Exception` or does something with the failure (logs it, records it, returns a
fallback). A comment is not something.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_BROAD = {"Exception", "BaseException"}

#: path -> enclosing function -> why the swallow stays. Keep this short.
ALLOWED: dict[str, dict[str, str]] = {
    "robothor/autonomy/workflows/service.py": {
        "_report_startup_failure": (
            "logging is what just failed; the SystemExit that follows carries the "
            "exception name and the unit still enters `failed`"
        ),
    },
    "robothor/engine/tools/dispatch.py": {
        "_audit_tool_call": (
            "the tool-call audit write is being reworked separately; it must not "
            "fail the tool call, and changing it here would collide with that work"
        ),
    },
}


def _is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    name = parts[-1]
    return (
        "tests" in parts
        or name.startswith("test_")
        or name == "conftest.py"
        or name.startswith("conftest_")
    )


def _tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if p and not _is_test_path(p)]


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, ast.Name) and n.id in _BROAD for n in names)


def _silent_broad_handlers(source: str) -> list[tuple[int, str]]:
    """(line, enclosing function) of every broad handler whose body is only `pass`."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if (
                isinstance(child, ast.ExceptHandler)
                and _is_broad(child)
                and all(isinstance(stmt, ast.Pass) for stmt in child.body)
            ):
                found.append((child.lineno, func))
            visit(child, name)

    visit(tree, "<module>")
    return found


class TestTheGateItself:
    def test_it_finds_a_bare_pass(self) -> None:
        src = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
        assert _silent_broad_handlers(src) == [(4, "f")]

    def test_it_finds_a_bare_except_and_a_tuple(self) -> None:
        src = (
            "try:\n    g()\nexcept:\n    pass\n"
            "try:\n    g()\nexcept (ValueError, Exception):\n    pass\n"
        )
        assert [line for line, _ in _silent_broad_handlers(src)] == [3, 7]

    def test_a_narrow_type_or_a_log_line_passes(self) -> None:
        src = (
            "try:\n    g()\nexcept ValueError:\n    pass\n"
            "try:\n    g()\nexcept Exception as e:\n    logger.debug('g failed: %s', e)\n"
        )
        assert _silent_broad_handlers(src) == []


def test_no_broad_except_pass_outside_tests() -> None:
    offenders: list[str] = []
    for rel in _tracked_python_files():
        try:
            source = (REPO_ROOT / rel).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if "except" not in source or "pass" not in source:
            continue  # cheap pre-filter; parsing all ~1,300 files is the slow part
        try:
            sites = _silent_broad_handlers(source)
        except SyntaxError:
            continue
        allowed = ALLOWED.get(rel, {})
        offenders.extend(f"{rel}:{line} in {func}()" for line, func in sites if func not in allowed)
    assert not offenders, (
        "broad `except ...: pass` swallows a failure with no trace. Catch the "
        "narrow type you expect, or log the reason with context:\n  " + "\n  ".join(offenders)
    )


def test_every_allowlisted_site_still_exists() -> None:
    """An allowlist entry whose swallow is gone is a hole waiting for the next one."""
    stale: list[str] = []
    for rel, funcs in ALLOWED.items():
        sites = {func for _, func in _silent_broad_handlers((REPO_ROOT / rel).read_text())}
        stale.extend(f"{rel}::{func}" for func in funcs if func not in sites)
    assert not stale, f"remove these from ALLOWED, the swallow is gone: {stale}"


def test_the_sandbox_boot_source_swallows_only_its_documented_fail_open_installs() -> None:
    """The in-sandbox boot file is a string template, so the file scan cannot see it.

    It has no logger (it runs under an isolated interpreter before the agent's
    snippet), so the rule there is narrower: the PR_SET_CHILD_SUBREAPER call
    catches what a platform without prctl raises, and only the two recorder
    installs, whose fail-open contract is written beside them, stay broad.
    """
    from robothor.engine.sandbox_runtime.boot_template import BOOT_TEMPLATE

    rendered = BOOT_TEMPLATE.format(
        guarded={"robothor"},
        subreaper=36,
        recorder="example_http_recorder",
        record_file="http.jsonl",
        spawn_recorder="example_spawn_recorder",
        spawn_record_file="spawn.jsonl",
    )
    lines = rendered.splitlines()
    swallowed = [
        lines[line - 2].strip() for line, _ in _silent_broad_handlers(rendered)
    ]  # the statement just above each handler is what it guards
    assert swallowed == [
        "_genus_http.install(os.path.join(_DIR, 'http.jsonl'))",
        "_genus_spawn.install(os.path.join(_DIR, 'spawn.jsonl'))",
    ]
