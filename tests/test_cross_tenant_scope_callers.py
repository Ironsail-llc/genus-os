"""Entering another tenant's scope from an agent run is pinned to its call SITES.

``tenant_scope(t)`` binds every connection checked out inside it to tenant
``t``. From the engine and the tenancy package -- everything an agent run can
reach -- that is how a parent tenant's agent could read and write a child's
rows, so each call site is an authorisation boundary. The binding for an
agent's cross-tenant call lives in ``robothor/engine/tools/cross_tenant.py``
(``_scope``, reached through ``target_scope`` and ``run_handler``, every one
behind ``cross_tenant.admit``); every other site binds the run's OWN tenant, a
sandbox, or the platform tenant, and says so in ``EXPECTED_TENANT_SCOPE``.

Pinned per call site, not per function name: the key is the file, the
enclosing function's qualified name (``<module>`` at module level) and how many
times the helper is called there, so a second call inside a listed function,
a module-level call and an aliased import (``tenant_scope as ts``) all red this
test. Callers of ``cross_tenant``'s three scope entry points are pinned
repo-wide (``robothor/``, ``crm/``).

A new site reds this test and has to explain itself: route an agent's
crossing through ``cross_tenant``, or justify the site and add it here.

Modelled on ``tests/test_unbound_scope_callers.py``.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Everything an agent run can reach that might enter another tenant's scope.
TENANT_SCOPE_TREES = ("robothor/engine", "robothor/tenancy")
TENANT_SCOPE = "tenant_scope"

#: Where callers of cross_tenant's scope entry points are looked for.
CROSS_TENANT_TREES = ("robothor", "crm")
CROSS_TENANT_MODULE = "robothor.engine.tools.cross_tenant"
CROSS_TENANT_FILE = "robothor/engine/tools/cross_tenant.py"
CROSS_TENANT_HELPERS = frozenset({"target_scope", "run_handler", "_scope"})

#: ``file::qualified function`` -> how many ``tenant_scope`` calls it makes.
EXPECTED_TENANT_SCOPE: dict[str, int] = {
    # ── The one binding for an agent's cross-tenant call ──
    # Reached only through target_scope / run_handler / a dry run's read, each
    # behind cross_tenant.admit (pinned in EXPECTED_CROSS_TENANT).
    "robothor/engine/tools/cross_tenant.py::_scope": 1,
    # The rollup reads each tenant it is GIVEN in that tenant's scope; its
    # engine caller (handlers/org.py) passes cross_tenant.reachable_tenants.
    "robothor/tenancy/rollup.py::tenant_rollup": 1,
    # ── Benchmark sandbox: bound to the sandbox tenant, never a real child ──
    "robothor/engine/tools/handlers/benchmark.py::_execute_task_run": 1,
    "robothor/engine/benchmark_sandbox.py::_check_rows_match": 1,
    "robothor/engine/benchmark_sandbox.py::count_rows": 1,
    "robothor/engine/benchmark_sandbox.py::ensure_sandbox_tenant": 1,
    "robothor/engine/benchmark_sandbox.py::insert_row": 1,
    "robothor/engine/benchmark_sandbox.py::read_row": 1,
    "robothor/engine/benchmark_sandbox.py::teardown_sandbox": 1,
    "robothor/engine/benchmark_sandbox.py::update_row": 1,
    # ── The authenticated chat caller's OWN tenant (auth.tenant_id) ──
    "robothor/engine/chat_delivery.py::_saved_request": 1,
    "robothor/engine/chat_delivery.py::_saved_result": 1,
    "robothor/engine/chat_plan_changes.py::replace_pending": 1,
    "robothor/engine/chat_plan_claim.py::already_admitted": 1,
    "robothor/engine/chat_plan_claim.py::claim": 1,
    "robothor/engine/chat_plan_claim.py::clear_claim": 1,
    "robothor/engine/chat_recovery.py::read_outcome": 1,
    # ── The durable effect ledger and run controls, in the run's OWN tenant
    # (context.tenant_id), or a record read back in the tenant that wrote it ──
    "robothor/engine/runtime/chat_effect_recovery.py::reconcile_record": 1,
    "robothor/engine/runtime/classification_audit.py::_has_run": 1,
    "robothor/engine/runtime/classified_deadline.py::_persist": 1,
    "robothor/engine/runtime/controls.py::issue": 1,
    "robothor/engine/runtime/controls.py::issue_request": 1,
    "robothor/engine/runtime/controls.py::stopped": 1,
    "robothor/engine/runtime/effect_recovery.py::_sweep_terminal": 1,
    "robothor/engine/runtime/effect_recovery.py::reconcile_unverifiable": 1,
    "robothor/engine/runtime/effect_results.py::record": 1,
    "robothor/engine/runtime/effect_tools.py::sweep": 2,
    "robothor/engine/runtime/effects.py::_release": 1,
    "robothor/engine/runtime/effects.py::abandon_run": 1,
    "robothor/engine/runtime/effects.py::begin": 1,
    "robothor/engine/runtime/effects.py::finish": 1,
    "robothor/engine/runtime/effects.py::mark_dispatched": 1,
    "robothor/engine/runtime/effects.py::operator_resolve": 1,
    "robothor/engine/runtime/effects.py::read": 1,
    "robothor/engine/runtime/effects.py::resolve": 1,
    "robothor/engine/runtime/effects.py::unresolved": 1,
    "robothor/engine/runtime/note_recovery.py::sweep": 2,
    "robothor/engine/runtime/note_recovery.py::verify": 1,
    "robothor/engine/runtime/task_receipts.py::remember_existing": 1,
    "robothor/engine/runtime/task_recovery.py::sweep": 2,
    "robothor/engine/runtime/task_recovery.py::verify": 1,
    # ── The remote-tool request ledger, in this engine's OWN tenant (the
    # authenticated bridge token's, which engine/auth.py pins to this engine) ──
    "robothor/engine/remote_tools.py::claim_request": 1,
    "robothor/engine/remote_tools.py::finish_request": 1,
    "robothor/engine/remote_tools.py::release_request": 1,
    # ── Other fixed bindings ──
    # A coding job's rows, in the tenant that owns the job.
    "robothor/engine/coding/jobs.py::PgJobStore._connect._scoped": 1,
    # A federated peer's op, in the tenant its connection was granted.
    "robothor/engine/federation_responder.py::make_command_handler.handle": 1,
    # Provider keys are the instance's: always the platform tenant's vault.
    "robothor/engine/key_pool.py::_vault_export": 1,
    # deep_reason's memory helpers, in the run's OWN tenant: DeepReasonConfig.
    # tenant_id is ctx.tenant_id (deep_reason is in neither cross-tenant
    # allowlist, so admit refuses a `tenant` argument and ctx is never swapped)
    # or the deep-mode run's tenant, resolved once by resolve_acting_tenant and
    # closed over by each helper; the helpers take no tenant argument.
    "robothor/engine/rlm_tool.py::_as_tenant": 1,
}

#: ``file::qualified function`` -> calls of target_scope / run_handler / _scope.
EXPECTED_CROSS_TENANT: dict[str, int] = {
    # Inside cross_tenant itself: the dry run's read, the public entry point,
    # and the handler run, each after admit.
    "robothor/engine/tools/cross_tenant.py::_current_record": 1,
    "robothor/engine/tools/cross_tenant.py::target_scope": 1,
    "robothor/engine/tools/cross_tenant.py::run_handler": 1,
    # Dispatch, after cross_tenant.admit returned a Crossing: the handler run
    # and the post-condition readback where the write landed.
    "robothor/engine/tools/dispatch.py::_dispatch_admitted": 2,
}


def _files(trees: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for top in trees:
        out.extend(sorted((REPO_ROOT / top).rglob("*.py")))
    return [
        p
        for p in out
        if "/tests/" not in p.as_posix()
        and not p.name.startswith("test_")
        and "node_modules" not in p.parts
    ]


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain, else ``""``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


class _Sites(ast.NodeVisitor):
    """Counts calls matching ``is_target`` per enclosing qualified function."""

    def __init__(self, rel: str, is_target: object) -> None:
        self.rel = rel
        self.is_target = is_target
        self.stack: list[str] = []
        self.found: Counter[str] = Counter()

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        if self.is_target(node.func):  # type: ignore[operator]
            where = ".".join(self.stack) or "<module>"
            self.found[f"{self.rel}::{where}"] += 1
        self.generic_visit(node)


def _imports(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """Names bound to cross_tenant's helpers, and names bound to the module itself."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    parent, _, leaf = CROSS_TENANT_MODULE.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == CROSS_TENANT_MODULE:
            for alias in node.names:
                if alias.name in CROSS_TENANT_HELPERS:
                    names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == parent:
            modules.update(alias.asname or leaf for alias in node.names if alias.name == leaf)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == CROSS_TENANT_MODULE:
                    modules.add(alias.asname or CROSS_TENANT_MODULE)
    return names, modules


def _tenant_scope_aliases(tree: ast.AST) -> set[str]:
    aliases = {TENANT_SCOPE}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(a.asname for a in node.names if a.name == TENANT_SCOPE and a.asname)
    return aliases


def _tenant_scope_sites() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in _files(TENANT_SCOPE_TREES):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _tenant_scope_aliases(tree)

        def is_target(func: ast.AST, aliases: set[str] = aliases) -> bool:
            if isinstance(func, ast.Name):
                return func.id in aliases
            return isinstance(func, ast.Attribute) and func.attr == TENANT_SCOPE

        visitor = _Sites(path.relative_to(REPO_ROOT).as_posix(), is_target)
        visitor.visit(tree)
        found.update(visitor.found)
    return found


def _cross_tenant_sites() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in _files(CROSS_TENANT_TREES):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names, modules = _imports(tree)
        if rel == CROSS_TENANT_FILE:
            names.update({n: n for n in CROSS_TENANT_HELPERS})

        def is_target(
            func: ast.AST, names: dict[str, str] = names, modules: set[str] = modules
        ) -> bool:
            if isinstance(func, ast.Name):
                return func.id in names
            if isinstance(func, ast.Attribute) and func.attr in CROSS_TENANT_HELPERS:
                owner = _dotted(func.value)
                return owner in modules or owner == CROSS_TENANT_MODULE
            return False

        visitor = _Sites(rel, is_target)
        visitor.visit(tree)
        found.update(visitor.found)
    return found


def _compare(found: Counter[str], expected: dict[str, int], what: str) -> None:
    extra = {k: v for k, v in found.items() if expected.get(k, 0) < v}
    missing = {k: v for k, v in expected.items() if found.get(k, 0) < v}
    assert not extra, (
        f"new or additional call site(s) of {what}: "
        f"{ {k: f'{found[k]} (pinned {expected.get(k, 0)})' for k in sorted(extra)} }. "
        "Each one can enter another tenant's rows. Route an agent's crossing through "
        "robothor/engine/tools/cross_tenant.py, or justify the site and pin it here."
    )
    assert not missing, f"pinned call site(s) of {what} are gone; unpin them: {missing}"


def test_the_scanners_find_their_helpers_at_all() -> None:
    assert _tenant_scope_sites(), "no tenant_scope call found -- the scan is broken"
    assert _cross_tenant_sites(), "no cross_tenant scope call found -- the scan is broken"


def test_exactly_the_audited_tenant_scope_sites() -> None:
    _compare(_tenant_scope_sites(), EXPECTED_TENANT_SCOPE, TENANT_SCOPE)


def test_exactly_the_audited_cross_tenant_scope_callers() -> None:
    _compare(_cross_tenant_sites(), EXPECTED_CROSS_TENANT, "cross_tenant's scope entry points")


# ── The scanner itself ─────────────────────────────────────────────────────


def _scan_source(source: str) -> tuple[Counter[str], Counter[str]]:
    tree = ast.parse(source)
    aliases = _tenant_scope_aliases(tree)
    names, modules = _imports(tree)

    def ts(func: ast.AST) -> bool:
        if isinstance(func, ast.Name):
            return func.id in aliases
        return isinstance(func, ast.Attribute) and func.attr == TENANT_SCOPE

    def ct(func: ast.AST) -> bool:
        if isinstance(func, ast.Name):
            return func.id in names
        if isinstance(func, ast.Attribute) and func.attr in CROSS_TENANT_HELPERS:
            return _dotted(func.value) in modules or _dotted(func.value) == CROSS_TENANT_MODULE
        return False

    a, b = _Sites("x.py", ts), _Sites("x.py", ct)
    a.visit(tree)
    b.visit(tree)
    return a.found, b.found


def test_the_scanner_counts_a_second_call_in_one_function() -> None:
    found, _ = _scan_source(
        "from robothor.db.connection import tenant_scope\n"
        "def f(t):\n    with tenant_scope(t):\n        pass\n    with tenant_scope(t):\n        pass\n"
    )
    assert found == Counter({"x.py::f": 2})


def test_the_scanner_sees_aliases_and_module_level_calls() -> None:
    found, ct = _scan_source(
        "from robothor.db.connection import tenant_scope as ts\n"
        "import robothor.engine.tools.cross_tenant as ct\n"
        "from robothor.engine.tools.cross_tenant import run_handler as rh\n"
        "ts('acme-child-a')\n"
        "class C:\n    def m(self):\n        ct.target_scope(None)\n        rh()\n"
    )
    assert found == Counter({"x.py::<module>": 1})
    assert ct == Counter({"x.py::C.m": 2})
