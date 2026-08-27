"""FleetPool refused nothing in production for as long as it has existed.

`pool.py` has can_start/register_run/complete_run, a 12-test suite, and the
daemon initialises it and LOGS the cap it enforces — `max_concurrent=3`. It has
never had a production caller. `leader.py:8` asserts "Dedup (redis-backed) and
the FleetPool are the real correctness boundary". Only half of that is true.

The cost was measured on 2026-08-27: with the fleet on a single-GPU local tier
serving OLLAMA_NUM_PARALLEL=2, the box ran up to 12 concurrent agent runs
against its configured cap of 3, and the operator's own Telegram turns queued
third in line behind background jobs — 1.2 min with nothing else running,
17.9 min with two background runs in flight.

The reservation, not the cap, is what fixes that. A cap alone still lets
background work fill every slot; a slot held back for interactive traffic makes
the inversion structurally impossible.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from robothor.engine.pool import FleetPool, Priority


class TestTheReservationDesignsOutTheInversion:
    def test_default_reserved_slots_preserves_existing_behaviour(self):
        """The 12 existing tests must keep passing byte-for-byte."""
        p = FleetPool(max_concurrent=3)
        for i in range(3):
            assert p.can_start(f"a{i}")[0]
            p.register_run(f"r{i}", f"a{i}")
        assert not p.can_start("a4")[0]

    def test_background_cannot_take_the_last_reserved_slot(self):
        p = FleetPool(max_concurrent=2, reserved_slots=1)
        p.register_run("r1", "bg1")
        allowed, reason = p.can_start("bg2", priority=Priority.BACKGROUND)
        assert not allowed
        assert "reserved" in reason.lower()

    def test_critical_may_use_a_reserved_slot(self):
        p = FleetPool(max_concurrent=2, reserved_slots=1)
        p.register_run("r1", "bg1")
        assert p.can_start("main", priority=Priority.CRITICAL)[0]

    def test_interactive_bypasses_admission_entirely(self):
        """The priority-inversion proof: a human waiting is never queued."""
        p = FleetPool(max_concurrent=2, reserved_slots=1)
        p.register_run("r1", "bg1")
        p.register_run("r2", "bg2")
        assert p.active_count >= p._max_concurrent
        assert p.can_start("main", priority=Priority.INTERACTIVE)[0]

    def test_set_limits_retunes_the_live_singleton(self):
        """Switching to the local tier must not need a restart."""
        p = FleetPool(max_concurrent=5)
        p.register_run("r1", "a1")
        p.register_run("r2", "a2")
        assert p.can_start("a3", priority=Priority.BACKGROUND)[0]
        p.set_limits(max_concurrent=2, reserved_slots=1)
        assert not p.can_start("a3", priority=Priority.BACKGROUND)[0]

    def test_a_released_slot_is_reusable(self):
        """Asymmetric accounting is how an admission control becomes the outage."""
        p = FleetPool(max_concurrent=1)
        p.register_run("r1", "a1")
        assert not p.can_start("a2")[0]
        p.complete_run("r1")
        assert p.can_start("a2")[0]


class TestItHasAProductionCaller:
    """The guard that would have caught the original defect.

    Styled after test_plugin_groups_are_consumed.py, which is the one
    anti-inertness pattern in this repo that demonstrably works.
    """

    @pytest.mark.parametrize("method", ["can_start", "register_run", "complete_run"])
    def test_the_scheduler_calls_it(self, method):
        engine = Path(__file__).resolve().parents[1]
        callers = []
        for py in engine.rglob("*.py"):
            if "/tests/" in str(py) or py.name in {"pool.py", "__init__.py"}:
                continue
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            callers.extend(
                f"{py.name}:{n.lineno}"
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == method
            )
        assert callers, (
            f"FleetPool.{method} has no production caller — the daemon logs a cap "
            f"that nothing enforces, which is the state this test exists to end"
        )
