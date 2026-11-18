"""Entering another tenant's scope from engine tool code is pinned to its call sites.

``tenant_scope(t)`` binds every connection checked out inside it to tenant
``t``. From engine tool code, and from the organisation rollup the tools and
the dashboard share, that is how a parent tenant's agent reads and writes a
child's rows -- so each call site is an authorisation boundary, and every one
of them must sit behind ``robothor.engine.tools.cross_tenant``'s checks. A new
caller reds this test and has to explain itself.

Modelled on ``tests/test_unbound_scope_callers.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("robothor/engine/tools", "robothor/tenancy/rollup.py")
HELPER = "tenant_scope"

EXPECTED_CALLERS: set[str] = {
    # The one swap for a cross-tenant tool call, after cross_tenant.admit.
    "robothor/engine/tools/cross_tenant.py::run_handler",
    # A dry-run update reads the record it would change, after admit.
    "robothor/engine/tools/cross_tenant.py::_current_record",
    # The rollup reads each tenant it is GIVEN in that tenant's scope; its
    # engine caller (handlers/org.py) passes cross_tenant.reachable_tenants.
    "robothor/tenancy/rollup.py::tenant_rollup",
    # The benchmark harness binds a graded run to the sandbox tenant, never a
    # real child (handlers/benchmark.py).
    "robothor/engine/tools/handlers/benchmark.py::_execute_task_run",
}


def _files() -> list[Path]:
    out: list[Path] = []
    for entry in SCANNED:
        path = REPO_ROOT / entry
        out.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return [p for p in out if "/tests/" not in str(p) and not p.name.startswith("test_")]


def _callers() -> set[str]:
    found: set[str] = set()
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else (func.attr if isinstance(func, ast.Attribute) else None)
                )
                if name == HELPER:
                    found.add(f"{rel}::{node.name}")
    return found


def test_the_scanner_finds_the_helper_at_all() -> None:
    assert _callers(), f"no call site of {HELPER} found -- the scan is broken or it was renamed"


def test_exactly_the_audited_callers() -> None:
    found = _callers()
    extra = sorted(found - EXPECTED_CALLERS)
    missing = sorted(EXPECTED_CALLERS - found)
    assert not extra, (
        f"new caller(s) of {HELPER} in engine tool code: {extra}. Each one enters "
        "another tenant's rows. Route it through robothor/engine/tools/cross_tenant.py, "
        "or justify it and add it to EXPECTED_CALLERS."
    )
    assert not missing, f"expected caller(s) no longer use {HELPER}: {missing}"
