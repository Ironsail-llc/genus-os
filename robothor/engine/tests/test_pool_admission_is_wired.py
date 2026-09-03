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
        """Asymmetric accounting is how an admission control becomes the outage.

        BACKGROUND priority deliberately: at a one-slot cap CRITICAL gets a
        bounded slot of overflow (see test_admission_starvation.py), so it is
        the wrong probe for "is the slot actually released".
        """
        p = FleetPool(max_concurrent=1)
        p.register_run("r1", "a1")
        assert not p.can_start("a2", priority=Priority.BACKGROUND)[0]
        p.complete_run("r1")
        assert p.can_start("a2", priority=Priority.BACKGROUND)[0]


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


class TestTheSpawnPathNeverGatesOnAdmission:
    """The inverse of the guard above, and the more dangerous direction.

    A child agent queueing for a fleet slot its own parent is holding is a
    deadlock: the parent cannot release until the child returns, and the child
    cannot start until the parent releases. So the spawn path is bounded by a
    LIMIT (`max_concurrent_spawns`, narrowed to the device's policy) and must
    never call the admission gate.

    "Must never" is the sort of property that decays silently — the deadlock
    only shows up under load, on the tier where slots are scarce. Asserting it
    from the source is the cheapest way to keep it true.
    """

    FORBIDDEN = ("admit", "can_start")

    def _spawn_source(self) -> Path:
        path = Path(__file__).resolve().parents[1] / "tools" / "handlers" / "spawn.py"
        assert path.exists(), f"spawn handler moved — re-point this guard: {path}"
        return path

    @pytest.mark.parametrize("method", FORBIDDEN)
    def test_spawn_does_not_call_the_admission_gate(self, method):
        tree = ast.parse(self._spawn_source().read_text())
        callers = [
            f"spawn.py:{n.lineno}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == method)
                or (isinstance(n.func, ast.Name) and n.func.id == method)
            )
        ]
        assert not callers, (
            f"spawn.py calls {method}() at {callers} — a spawned child must not "
            f"queue for a slot its parent is holding. Bound the fan-out with "
            f"max_concurrent_spawns instead; that is what set_max_concurrent_spawns exists for"
        )
