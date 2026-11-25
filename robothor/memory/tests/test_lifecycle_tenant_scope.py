"""Every per-tenant step of the nightly pass runs inside that tenant's scope.

With row-level security on, a connection binds to the tenant of the
``tenant_scope`` it is taken in, else to the process's own tenant. A step that
passes ``tenant_id=child`` but runs outside ``tenant_scope(child)`` therefore
reads nothing of the child at the database, and the child is never maintained.
The SQL side is covered against Postgres in
``tests/integration/test_memory_maintenance_tenancy.py``; this pins the scope
for steps 8-13, whose functions are stubbed here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.db.connection import current_tenant_scope

TENANTS = ["test-tenant-a", "test-tenant-b"]


@pytest.mark.asyncio
async def test_every_per_tenant_step_runs_inside_that_tenants_scope(monkeypatch):
    from robothor.memory import lifecycle

    seen: dict[str, list[tuple[Any, Any]]] = {}

    def _record(step: str, tenant: Any, result: Any) -> Any:
        seen.setdefault(step, []).append((tenant, current_tenant_scope()))
        return result

    async def _core(tid, *_a, **_kw):
        return _record("core", tid, {"insights": {}})

    async def _episodes(**kw):
        return _record("episodes", kw.get("tenant_id"), {})

    async def _prefs(**kw):
        return _record("preferences", kw.get("tenant_id"), {})

    async def _drift(**kw):
        return _record("drift", kw.get("tenant_id"), {})

    async def _promote(**kw):
        return _record("promote", kw.get("tenant_id"), {})

    monkeypatch.setattr(lifecycle, "active_tenant_ids", lambda: list(TENANTS))
    monkeypatch.setattr(lifecycle, "_run_tenant_core_steps", _core)
    monkeypatch.setattr(lifecycle, "_release_lifecycle_lock", lambda: None)
    monkeypatch.setattr(lifecycle, "_intent_populate_enabled", lambda: False)

    with (
        patch("redis.Redis", side_effect=RuntimeError("no redis in a unit test")),
        patch("robothor.memory.episodes.build_episodes_from_facts", _episodes),
        patch("robothor.memory.preferences.extract_preferences_from_facts", _prefs),
        patch("robothor.memory.preferences.detect_drift", _drift),
        patch(
            "robothor.engine.chat_store.cleanup_stale_chat_turns",
            lambda days, tid: _record("chat_ttl", tid, 0),
        ),
        patch(
            "robothor.memory.breadcrumbs.prune_expired_breadcrumbs",
            lambda tid: _record("breadcrumb_prune", tid, 0),
        ),
        patch("robothor.memory.breadcrumbs.promote_hot_breadcrumbs", _promote),
        patch(
            "robothor.memory.outcomes.cleanup_old_access_logs",
            lambda days, tid: _record("access_log", tid, 0),
        ),
    ):
        await lifecycle.run_lifecycle_maintenance()

    expected = [(t, t) for t in TENANTS]
    for step in (
        "core",
        "episodes",
        "preferences",
        "drift",
        "chat_ttl",
        "breadcrumb_prune",
        "promote",
        "access_log",
    ):
        assert seen.get(step) == expected, f"step {step!r} ran as {seen.get(step)}"
