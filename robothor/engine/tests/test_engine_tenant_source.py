"""The engine learns its tenant from ``EngineConfig``, never from the environment.

Several engines (one per tenant) share one database and one Redis. A module
that reads ``ROBOTHOR_TENANT_ID`` itself answers "which tenant is this
process?", which is the wrong question inside a run scoped to another tenant
(a benchmark sandbox, a warmup for a named tenant) and a second source of truth
that can disagree with the config the rest of the engine uses.
``engine/config.py`` reads it once; everything else takes ``config.tenant_id``
or the run's tenant. ``robothor/db/connection.py`` keeps its own read as the
process-level fallback for connection binding -- it is outside this package.
"""

from __future__ import annotations

import ast
from pathlib import Path

from robothor.db.connection import tenant_scope

ENGINE_DIR = Path(__file__).resolve().parents[1]
VAR = "ROBOTHOR_TENANT_ID"
ALLOWED = {"robothor/engine/config.py"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _reads_var(node: ast.AST) -> bool:
    """``os.environ.get(VAR…)``, ``os.getenv(VAR…)`` or a load of ``os.environ[VAR]``."""
    if isinstance(node, ast.Call) and node.args:
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == VAR):
            return False
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and _is_os_environ(func.value):
            return True
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        )
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        key = node.slice
        return _is_os_environ(node.value) and isinstance(key, ast.Constant) and key.value == VAR
    return False


def _readers() -> set[str]:
    found: set[str] = set()
    for path in sorted(ENGINE_DIR.rglob("*.py")):
        rel = path.relative_to(ENGINE_DIR)
        if "tests" in rel.parts or path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _reads_var(node):
                found.add(f"robothor/engine/{rel.as_posix()}:{node.lineno}")
    return found


class TestOnlyTheConfigReadsTheTenantFromTheEnvironment:
    def test_no_engine_module_but_config_reads_the_tenant_env_var(self):
        offenders = sorted(r for r in _readers() if r.rsplit(":", 1)[0] not in ALLOWED)
        assert not offenders, (
            f"read {VAR} through EngineConfig.tenant_id (or the run's tenant) instead:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_scan_still_sees_the_config_read(self):
        """Guards the guard: if the AST match broke, the test above would pass vacuously."""
        assert any(r.startswith("robothor/engine/config.py:") for r in _readers())


class TestWarmupHooksUseTheRunsTenant:
    """Context hooks take no arguments; the warmup tells them whose run it is."""

    def test_travel_status_reads_the_warmup_tenant(self, monkeypatch):
        from unittest.mock import patch

        from robothor.engine.warmup import _travel_status, set_warmup_tenant

        monkeypatch.setenv(VAR, "acme-parent")
        with (
            patch("robothor.memory.blocks.read_block", return_value={"content": ""}) as read,
            set_warmup_tenant("acme-child-a"),
        ):
            _travel_status()
        assert read.call_args.kwargs["tenant_id"] == "acme-child-a"

    def test_thread_pool_reads_the_warmup_tenant(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import patch

        from robothor.engine.thread_pool import _thread_pool_context
        from robothor.engine.warmup import set_warmup_kind, set_warmup_tenant

        monkeypatch.setenv(VAR, "acme-parent")
        with (
            patch("robothor.engine.thread_pool.list_threads", return_value=[]) as listed,
            patch("robothor.engine.thread_pool.auto_close_completed_threads", return_value=[]),
            patch("robothor.engine.thread_planner.plan_all_stalled", return_value=[]),
            set_warmup_kind("cron"),
            set_warmup_tenant("acme-child-a"),
        ):
            _thread_pool_context(SimpleNamespace(id="main"))
        assert listed.call_args.kwargs["tenant_id"] == "acme-child-a"

    def test_the_preamble_sets_the_warmup_tenant_for_its_hooks(self):
        from robothor.engine import warmup

        seen: list[str] = []

        def _hook() -> None:
            seen.append(warmup.current_warmup_tenant())

        warmup._CONTEXT_HOOKS.append(_hook)
        try:
            warmup._run_context_hooks(tenant_id="acme-child-a")
        finally:
            warmup._CONTEXT_HOOKS.remove(_hook)
        assert seen == ["acme-child-a"]

    def test_outside_a_warmup_the_tenant_is_the_scope_or_default(self):
        from robothor import constants
        from robothor.engine.warmup import current_warmup_tenant

        assert current_warmup_tenant() == constants.DEFAULT_TENANT
        with tenant_scope("acme-child-a"):
            assert current_warmup_tenant() == "acme-child-a"
