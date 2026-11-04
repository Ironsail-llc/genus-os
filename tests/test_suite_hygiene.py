"""Gates on the test suite itself, so it fails only for a real regression.

Two shapes made the suite red on a developer Mac or a loaded runner while CI
on an idle Linux box stayed green, and each costs a rerun that teaches people
to ignore red:

* A test that gives its own subprocess a long bound (``timeout=180``) while
  pytest kills the test at ``pytest.ini``'s 30 s. The test declared that the
  script may take three minutes and then refused to wait more than thirty
  seconds for it, so it failed whenever the machine was busy. The pytest
  timeout that applies to the call has to be at least the call's own bound.

* A shell pipeline that relies on GNU's reading of an option. ``paste -s``
  with no file operand reads stdin on GNU and prints a usage error on BSD and
  macOS, so the script broke there before it did any work. ``-`` names stdin
  for both.
"""

from __future__ import annotations

import ast
import configparser
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The modules ``subprocess`` exposes a ``timeout=`` on.
_SUBPROCESS_CALLS = frozenset({"run", "check_output", "check_call", "call"})


def _ini_timeout() -> float:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    return float(parser["pytest"].get("timeout", "0"))


def _is_timeout_mark(sub: ast.AST) -> bool:
    return (
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "timeout"
        and isinstance(sub.func.value, ast.Attribute)
        and sub.func.value.attr == "mark"
        and bool(sub.args)
        and isinstance(sub.args[0], ast.Constant)
        and isinstance(sub.args[0].value, int | float)
    )


def _timeout_marks(node: ast.AST) -> list[float]:
    """Every ``pytest.mark.timeout(N)`` literal inside ``node``."""
    return [
        float(sub.args[0].value)  # type: ignore[attr-defined]
        for sub in ast.walk(node)
        if _is_timeout_mark(sub)
    ]


def _module_timeout(tree: ast.Module) -> float | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in stmt.targets
        ):
            marks = _timeout_marks(stmt.value)
            if marks:
                return max(marks)
    return None


def _subprocess_timeouts(tree: ast.Module):
    """``(lineno, bound, enclosing decorated scopes)`` for each literal bound."""

    def visit(node: ast.AST, scopes: tuple[ast.AST, ...]):
        for child in ast.iter_child_nodes(node):
            inner = scopes
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = (*scopes, child)
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _SUBPROCESS_CALLS
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "subprocess"
            ):
                for keyword in child.keywords:
                    if (
                        keyword.arg == "timeout"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, int | float)
                    ):
                        yield child.lineno, float(keyword.value.value), inner
            yield from visit(child, inner)

    yield from visit(tree, ())


def _test_modules() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "tests", "robothor"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    return [
        REPO_ROOT / path
        for path in tracked
        if Path(path).name.startswith("test_") and path.endswith(".py")
    ]


def test_every_subprocess_bound_fits_inside_its_pytest_timeout() -> None:
    default = _ini_timeout()
    short: list[str] = []
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_timeout(tree)
        for lineno, bound, scopes in _subprocess_timeouts(tree):
            applied = module if module is not None else default
            for scope in scopes:
                marks = [mark for dec in scope.decorator_list for mark in _timeout_marks(dec)]
                if marks:
                    applied = max(marks)
            if applied and bound > applied:
                short.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno} gives its subprocess "
                    f"{bound:g}s but pytest stops the test at {applied:g}s"
                )
    assert not short, (
        "a test that allows its subprocess longer than pytest allows the test fails "
        "whenever the machine is busy; raise the module's pytest.mark.timeout to at "
        "least the bound:\n" + "\n".join(short)
    )


#: ``paste -s``/``-sd<x>`` with no file operand: the stage ends, or stdin is
#: only redirected (``paste -sd' ' <file`` is the same error on BSD).
_PASTE_WITHOUT_OPERAND = re.compile(
    r"\bpaste\s+-s(?:d\s*(?:'[^']*'|\"[^\"]*\"|[^\s'\"]))?\s*(?=[)|;`<]|$)"
)


def test_no_script_relies_on_gnu_paste_reading_stdin_without_an_operand() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "scripts", ".github", "infra", "docker"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    offenders = []
    for name in tracked:
        path = REPO_ROOT / name
        if path.suffix in {".py", ".md", ".json", ".png", ".svg"} or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _PASTE_WITHOUT_OPERAND.search(line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "BSD and macOS paste require a file operand; write `paste -sd<x> -` so stdin "
        "is named:\n" + "\n".join(offenders)
    )
