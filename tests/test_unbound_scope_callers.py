"""``unbound_scope`` is an RLS widening, so its call sites are pinned.

Inside it a connection binds ``app.tenant_id = ''`` -- the permissive branch of
the migration-081 policy -- and sees every tenant's rows. That is required for
exactly one job: a sign-in finding an account before anyone knows which tenant
it belongs to. Every other caller is a query that can read across tenants, so
adding one reds this test and forces the question to be asked out loud.

Modelled on ``test_benchmark_decontamination.py::
TestTheRlsWideningHasExactlyThreeCallers``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("robothor", "crm")
HELPER = "unbound_scope"

EXPECTED_CALLERS: set[str] = {
    "crm/bridge/routers/auth.py::sso_exchange",
    "crm/bridge/routers/auth.py::refresh",
    "crm/bridge/routers/auth.py::logout",
    "crm/bridge/routers/auth.py::local_login_route",
    "crm/bridge/routers/auth.py::switch_tenant",
    "robothor/auth/accounts.py::get_account_by_idp",
    "robothor/auth/accounts.py::jit_provision",
    "robothor/auth/accounts.py::consume_active_session",
}


def _callers() -> set[str]:
    found: set[str] = set()
    for top in SCANNED:
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover - unreadable file
                continue
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
    assert _callers(), f"no call site of {HELPER} found — the scan is broken or it was renamed"


def test_exactly_the_audited_callers() -> None:
    found = _callers()
    extra = sorted(found - EXPECTED_CALLERS)
    missing = sorted(EXPECTED_CALLERS - found)
    assert not extra, (
        f"new caller(s) of {HELPER}: {extra}. Each one is a query that reads "
        "every tenant's rows. Justify it, then add it to EXPECTED_CALLERS."
    )
    assert not missing, (
        f"expected caller(s) no longer use {HELPER}: {missing}. A sign-in that "
        "lost its unbound scope fails for every account outside the default "
        "tenant once RLS is on."
    )
