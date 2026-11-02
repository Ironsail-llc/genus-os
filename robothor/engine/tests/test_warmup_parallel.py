"""The interactive warmup preamble, built in parallel.

Run one after another, ``build_interactive_preamble`` costs the sum of its
sections: identity enrichment, the memory blocks, entity recall, the context
hooks, host state, main's task queue and fleet surfaces, and the agent goal.
It reads the blocks in one statement, batches the entity queries, holds the
identity enrichment for the chat session, times every section, and fans the
sections out once an agent's previous warmup had a slow one, so it costs
roughly its slowest section. Fanned out or in place, the text is the same,
byte for byte. There is no switch: this is the only warmup.

The fake database below charges 50 ms for every statement, which is what makes
the sum-versus-max difference measurable without a real Postgres.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine import warmup
from robothor.engine.warmup import _CONTEXT_HOOKS, build_interactive_preamble
from robothor.identity import enrichment
from robothor.identity.context import EnrichedIdentity, IdentityContext

STATEMENT_SECONDS = 0.05
TENANT = "test-tenant"

IDENTITY = IdentityContext(
    tenant_id=TENANT,
    channel="webchat",
    identifier="user-1",
    verified=True,
    display_name="Alice",
    role="owner",
    person_id="person-1",
)

BLOCKS: dict[str, tuple[str, datetime | None]] = {
    "persona": ("I am the operator's assistant.", datetime.now(UTC) - timedelta(hours=1)),
    "user_profile": ("The operator prefers short answers.", None),
    "user_model": ("x" * 1200, datetime.now(UTC) - timedelta(hours=48)),
    "working_context": ("Preparing the quarterly review.", datetime.now(UTC)),
    "example_report": ("All checks passed.", None),
}

FACTS: dict[str, list[str]] = {
    "Bob": ["Bob owns the billing service.", "Bob is on leave next week.", "Bob likes tea.", "4th"],
    "Example": ["Example project ships in March."],
    "Carol": [f"Carol fact {i} " + "y" * 300 for i in range(3)],
}

MESSAGE = "Did Bob update the Example plan with Carol yesterday?"


class FakeDB:
    """Postgres stand-in: every statement sleeps, and every statement is logged."""

    def __init__(self, delay: float = STATEMENT_SECONDS) -> None:
        self.statements: list[str] = []
        self.delay = delay
        self._lock = threading.Lock()
        #: When set, each warmup-section thread's first statement waits here:
        #: it only opens once that many sections are in flight at the same time.
        self.gate: threading.Barrier | None = None
        self._gated = threading.local()
        self.in_flight = 0
        self.max_in_flight = 0

    def charge(self, sql: str) -> None:
        # The deferred read-accounting write runs off the critical path on its
        # own thread; it is not a section, so it is not counted as in flight.
        counted = not threading.current_thread().name.startswith("warmup-deferred")
        with self._lock:
            self.statements.append(" ".join(sql.split()))
            if counted:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            gate = self.gate
            if (
                gate is not None
                and threading.current_thread().name.startswith("warmup-section")
                and not getattr(self._gated, "done", False)
            ):
                self._gated.done = True
                gate.wait()
            time.sleep(self.delay)
        finally:
            if counted:
                with self._lock:
                    self.in_flight -= 1

    def count(self, needle: str) -> int:
        with self._lock:
            return sum(1 for s in self.statements if needle in s)

    @contextmanager
    def connection(self, *a: Any, **kw: Any):
        yield _Conn(self)


class _Conn:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def cursor(self, cursor_factory: Any = None) -> _Cursor:
        return _Cursor(self._db, dict_rows=cursor_factory is not None)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _Cursor:
    def __init__(self, db: FakeDB, *, dict_rows: bool) -> None:
        self._db = db
        self._dict = dict_rows
        self._rows: list[Any] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> None:
        self._db.charge(sql)
        flat = " ".join(sql.split())
        lists = [p for p in params if isinstance(p, list | tuple)]
        if "agent_memory_blocks" in flat:
            self._rows = self._blocks(flat, params, lists)
        elif "memory_facts" in flat:
            self._rows = self._facts(flat, params, lists)
        else:
            self._rows = []

    @staticmethod
    def _blocks(flat: str, params: Any, lists: list[Any]) -> list[Any]:
        if flat.upper().startswith("SELECT"):
            return [(n, *BLOCKS[n]) for n in lists[0] if n in BLOCKS]
        if "RETURNING" in flat:
            name = params[1]
            return [BLOCKS[name]] if name in BLOCKS else []
        return []

    def _facts(self, flat: str, params: Any, lists: list[Any]) -> list[Any]:
        def rows_for(name: str) -> list[dict[str, Any]]:
            texts = FACTS.get(name, [])[:3]
            return [
                {"fact_text": t, "category": "c", "importance_score": 0.5, "person_id": None}
                for t in texts
            ]

        if lists:
            out: list[dict[str, Any]] = []
            for ordinal, name in enumerate(lists[0], start=1):
                out.extend({**r, "candidate_ordinal": ordinal} for r in rows_for(name))
            return out
        name = params[1] if "lower_entities" in flat else params[0]
        return rows_for(name)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


def _forget_fan_out() -> None:
    """Drop what the warmup remembers about each agent's previous sections."""
    getattr(warmup, "_slowest_section_by_agent", {}).clear()


def _remember_slow_sections() -> None:
    """Make the next warmup of ``main`` fan out, as after a slow previous one."""
    warmup._slowest_section_by_agent[f"{TENANT}:main"] = 1.0


@pytest.fixture
def db():
    """The fake DB wired under every section the interactive preamble runs."""
    fake = FakeDB()

    def _enrichment(person_id: str, tenant_id: str) -> EnrichedIdentity:
        fake.charge("SELECT job_title FROM crm_people")
        fake.charge("SELECT relationships FROM memory_relations")
        return EnrichedIdentity(company="Example Co", job_title="Engineer")

    def _host_state(agent_id: str, config: Any = None) -> str:
        fake.charge("SELECT host state")
        return "--- HOST STATE ---\nEngine up 3h."

    def _list_tasks(**kw: Any) -> list[dict[str, Any]]:
        fake.charge("SELECT FROM crm_tasks")
        return [
            {
                "id": "t1",
                "title": "Reply to Bob",
                "status": "TODO",
                "assignedToAgent": "main",
                "objective": None,
                "updatedAt": None,
            }
        ]

    def _fleet(**kw: Any) -> str:
        fake.charge("SELECT FROM fleet surfaces")
        return "--- FLEET SURFACES ---\n[@example-agent 09:00] done"

    def _goal(**kw: Any) -> str:
        fake.charge("SELECT FROM agent goals")
        return "--- AGENT GOAL ---\nKeep the operator informed."

    def _hook() -> str:
        fake.charge("SELECT hook")
        return "Date: 2026-01-02"

    saved = _CONTEXT_HOOKS.copy()
    _CONTEXT_HOOKS[:] = [_hook]
    enrichment.clear_cache()
    _forget_fan_out()
    env = dict(os.environ)
    env.pop("ROBOTHOR_DATA_SCOPING", None)
    env.pop("MEMORY_WARMUP_RECALL_V2", None)
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            patch("robothor.memory.blocks.get_connection", fake.connection),
            patch("robothor.db.get_connection", fake.connection),
            patch("robothor.identity.enrichment._build_enrichment", side_effect=_enrichment),
            patch("robothor.engine.host_state.host_state_section", side_effect=_host_state),
            patch("robothor.crm.dal.list_tasks", side_effect=_list_tasks),
            patch("robothor.engine.warmup._recent_fleet_surfaces", side_effect=_fleet),
            patch("robothor.engine.session_goal.build_agent_goal_context", side_effect=_goal),
        ):
            yield fake
    finally:
        _CONTEXT_HOOKS[:] = saved
        enrichment.clear_cache()
        _forget_fan_out()


def _build(
    fan_out: bool | None = None, timings: dict[str, float] | None = None
) -> tuple[str, float]:
    """One warmup of ``main``. ``fan_out`` forces the mode; ``None`` leaves it learned."""
    enrichment.clear_cache()
    if fan_out is True:
        _remember_slow_sections()
    elif fan_out is False:
        _forget_fan_out()
    start = time.perf_counter()
    text = build_interactive_preamble(
        "main",
        MESSAGE,
        include_blocks=True,
        tenant_id=TENANT,
        extra_memory_blocks=["example_report", "persona"],
        identity=IDENTITY,
        section_timings=timings,
    )
    return text, time.perf_counter() - start


class TestSameText:
    def test_fanned_out_text_is_byte_identical_to_in_place(self, db: FakeDB) -> None:
        sequential, _ = _build(fan_out=False)
        parallel, _ = _build(fan_out=True)
        assert parallel == sequential
        # The fixture exercised every section, so equality means something.
        for marker in (
            "--- CURRENT USER ---",
            "Example Co",
            "--- MEMORY BLOCKS ---",
            "[user_model] [STALE",
            "[example_report]",
            "--- RELEVANT CONTEXT ---",
            "Bob owns the billing service.",
            "--- SITUATIONAL CONTEXT ---",
            "--- HOST STATE ---",
            "OPEN TASKS",
            "--- FLEET SURFACES ---",
            "--- AGENT GOAL ---",
        ):
            assert marker in sequential, marker

    def test_section_order_is_preserved(self, db: FakeDB) -> None:
        parallel, _ = _build(fan_out=True)
        order = [
            "--- CURRENT USER ---",
            "--- MEMORY BLOCKS ---",
            "--- RELEVANT CONTEXT ---",
            "--- SITUATIONAL CONTEXT ---",
            "--- HOST STATE ---",
            "OPEN TASKS",
            "--- FLEET SURFACES ---",
            "--- AGENT GOAL ---",
        ]
        positions = [parallel.index(m) for m in order]
        assert positions == sorted(positions)

    def test_a_failing_section_is_dropped_the_same_way(self, db: FakeDB) -> None:
        with patch(
            "robothor.engine.host_state.host_state_section", side_effect=RuntimeError("boom")
        ):
            sequential, _ = _build(fan_out=False)
            parallel, _ = _build(fan_out=True)
        assert "--- HOST STATE ---" not in parallel
        assert parallel == sequential

    def test_truncation_is_identical(self, db: FakeDB) -> None:
        big = {**BLOCKS, **{f"extra_{i}": ("z" * 790, None) for i in range(8)}}
        with patch.dict(BLOCKS, big):
            enrichment.clear_cache()
            _forget_fan_out()
            kwargs: dict[str, Any] = {
                "include_blocks": True,
                "tenant_id": TENANT,
                "extra_memory_blocks": [f"extra_{i}" for i in range(8)],
                "identity": IDENTITY,
            }
            sequential = build_interactive_preamble("main", MESSAGE, **kwargs)
            enrichment.clear_cache()
            _remember_slow_sections()
            parallel = build_interactive_preamble("main", MESSAGE, **kwargs)
        assert sequential.endswith("[warmup truncated]")
        assert parallel == sequential


class TestFewerStatements:
    def test_memory_blocks_are_read_in_one_statement(self, db: FakeDB) -> None:
        _build()
        reads = [
            s
            for s in db.statements
            if "agent_memory_blocks" in s and ("RETURNING" in s or s.upper().startswith("SELECT"))
        ]
        assert len(reads) == 1, reads

    def test_read_accounting_still_happens_in_one_batched_write(self, db: FakeDB) -> None:
        _build()
        warmup.wait_for_deferred_writes(timeout=2.0)
        touches = [
            s
            for s in db.statements
            if s.upper().startswith("UPDATE AGENT_MEMORY_BLOCKS") and "RETURNING" not in s
        ]
        assert len(touches) == 1, touches

    def test_entity_candidates_are_one_statement(self, db: FakeDB) -> None:
        _build()
        assert db.count("memory_facts") == 1

    def test_v2_recall_is_one_statement_either_way(self, db: FakeDB) -> None:
        os.environ["MEMORY_WARMUP_RECALL_V2"] = "1"
        sequential, _ = _build(fan_out=False)
        assert db.count("memory_facts") == 1
        parallel, _ = _build(fan_out=True)
        assert parallel == sequential
        assert db.count("memory_facts") == 2
        assert "Bob owns the billing service." in parallel


class TestOverlapAndTiming:
    def test_fanned_out_sections_are_all_in_flight_at_once(self, db: FakeDB) -> None:
        """Structural, not wall-clock: in place, one statement runs at a time;
        fanned out, every section has started before any of them can finish."""
        _build(fan_out=False)
        assert db.max_in_flight == 1  # in place: strictly one after another

        sections = 8  # identity, blocks, entity, hooks, host, tasks, fleet, goal
        # Each section's first statement blocks until all eight are waiting, so
        # this only opens if they run concurrently; run in sequence, the first
        # one would wait alone and the barrier would time out and break.
        db.gate = threading.Barrier(sections, timeout=10)
        db.max_in_flight = 0
        timings: dict[str, float] = {}
        text, _ = _build(timings=timings)  # the last warmup had slow sections

        assert not db.gate.broken
        assert db.max_in_flight >= sections
        assert len(timings) == sections
        for marker in ("--- CURRENT USER ---", "--- MEMORY BLOCKS ---", "--- AGENT GOAL ---"):
            assert marker in text, marker

    def test_per_section_timings_are_recorded(self, db: FakeDB) -> None:
        timings: dict[str, float] = {}
        _build(timings=timings)
        assert set(timings) == {
            "identity",
            "memory_blocks",
            "entity_context",
            "context_hooks",
            "host_state",
            "open_tasks",
            "fleet_surfaces",
            "agent_goal",
        }
        assert all(v >= 0 for v in timings.values())

    def test_sections_run_in_place_are_timed_too(self, db: FakeDB) -> None:
        in_place: dict[str, float] = {}
        _build(fan_out=False, timings=in_place)
        fanned: dict[str, float] = {}
        _build(fan_out=True, timings=fanned)
        assert set(in_place) == set(fanned)
        assert len(in_place) == 8


class TestContextVars:
    def test_hooks_still_see_the_warmup_kind(self, db: FakeDB) -> None:
        seen: list[str | None] = []

        def _hook() -> str:
            seen.append(warmup.current_warmup_kind())
            return "kind seen"

        _CONTEXT_HOOKS[:] = [_hook]
        with warmup.set_warmup_kind("interactive"):
            build_interactive_preamble("example-agent", MESSAGE, tenant_id=TENANT)
        assert seen == ["interactive"]


class TestEventLoop:
    async def test_loop_stays_responsive_while_warmup_runs(self, db: FakeDB) -> None:
        gaps: list[float] = []
        done = asyncio.Event()

        async def _ticker() -> None:
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(_ticker())
        await asyncio.to_thread(
            build_interactive_preamble,
            "main",
            MESSAGE,
            tenant_id=TENANT,
            identity=IDENTITY,
        )
        done.set()
        await ticker
        assert gaps and max(gaps) < 0.1


class TestFollowUpEnrichment:
    def test_follow_up_uses_the_session_cache(self, db: FakeDB) -> None:
        first = warmup.enrich_identity_for_turn("main", IDENTITY)
        enrichment._cache.clear()  # the 60 s cache expiring must not matter
        second = warmup.enrich_identity_for_turn("main", IDENTITY)
        assert first == second
        assert db.count("crm_people") == 1

    def test_the_warmup_and_the_follow_up_share_one_lookup(self, db: FakeDB) -> None:
        build_interactive_preamble("main", MESSAGE, tenant_id=TENANT, identity=IDENTITY)
        enrichment._cache.clear()
        warmup.enrich_identity_for_turn("main", IDENTITY)
        assert db.count("crm_people") == 1


class TestRunnerUsesTheTurnHelper:
    def test_enrichment_call_for_turn_is_what_the_runner_runs(self) -> None:
        """The runner offloads ``warmup.enrichment_call_for_turn``'s callable
        (the runner-level test lives in test_runner.py, TestIdentityThreading);
        ``enrich_identity_for_turn`` is that callable applied."""
        fn, args = warmup.enrichment_call_for_turn("main", IDENTITY)
        assert fn is enrichment.enrich_identity_for_session
        assert args == ("main", IDENTITY)


def _hook_thread_recorder() -> list[str]:
    """Replace the context hook with one that records the thread it ran on."""
    names: list[str] = []

    def _hook() -> str:
        names.append(threading.current_thread().name)
        return "Date: 2026-01-02"

    _CONTEXT_HOOKS[:] = [_hook]
    return names


class TestFanOutOnlyWhenItPays:
    """Threads cost more than a fast database's queries save.

    On a fast Postgres the fan-out made the preamble slower (QA measured
    24.8 ms in place against 56.3 ms fanned out). The statement savings always
    apply; the sections fan out only once the agent's previous warmup had a
    slow section.
    """

    def test_fast_db_stays_on_the_calling_thread(self, db: FakeDB) -> None:
        db.delay = 0.002
        threads = _hook_thread_recorder()
        caller = threading.current_thread().name
        _build(fan_out=False)  # imports
        for _ in range(5):
            _build()
        assert set(threads) == {caller}, threads

    def test_fast_db_still_saves_statements(self, db: FakeDB) -> None:
        db.delay = 0.0
        _build(fan_out=False)
        warmup.wait_for_deferred_writes(timeout=2.0)
        assert db.count("memory_facts") == 1  # one entity query
        assert db.count("RETURNING") == 0  # no per-block write-to-read
        assert db.count("agent_memory_blocks") == 2  # one read, one deferred touch

    def test_slow_db_fans_out_from_the_second_warmup_and_is_faster(self, db: FakeDB) -> None:
        threads = _hook_thread_recorder()
        caller = threading.current_thread().name
        _build(fan_out=False)  # imports
        _, sequential_s = _build(fan_out=False)
        assert threads[-1] == caller  # nothing known yet: run in place
        timings: dict[str, float] = {}
        _, fanned_s = _build(timings=timings)
        assert threads[-1] != caller  # the last warmup had a slow section
        assert fanned_s < sequential_s
        assert fanned_s < 1.5 * max(timings.values())

    def test_the_fanned_out_hooks_still_see_the_warmup_kind(self, db: FakeDB) -> None:
        seen: list[str | None] = []

        def _hook() -> str:
            seen.append(warmup.current_warmup_kind())
            return "kind seen"

        _CONTEXT_HOOKS[:] = [_hook]
        with warmup.set_warmup_kind("interactive"):
            build_interactive_preamble("main", MESSAGE, tenant_id=TENANT, identity=IDENTITY)
            build_interactive_preamble("main", MESSAGE, tenant_id=TENANT, identity=IDENTITY)
        assert seen == ["interactive", "interactive"]


class _InterpolatingCursor:
    """Formats each statement the way psycopg2's pyformat paramstyle does.

    A bare ``%`` that is not ``%s`` / ``%%`` consumes a parameter or raises;
    psycopg2 reports it as an IndexError at execute time.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = ()) -> None:
        self.statements.append(sql % tuple(repr(p) for p in params))

    def fetchall(self) -> list[Any]:
        return []


class TestEntitySqlInterpolates:
    @pytest.mark.parametrize("v2", ["0", "1"])
    def test_every_entity_statement_formats_with_its_params(self, v2: str) -> None:
        cur = _InterpolatingCursor()

        class _Conn:
            def cursor(self, cursor_factory: Any = None) -> _InterpolatingCursor:
                return cur

        @contextmanager
        def _conn(*a: Any, **kw: Any):
            yield _Conn()

        with (
            patch.dict(os.environ, {"MEMORY_WARMUP_RECALL_V2": v2}),
            patch("robothor.db.get_connection", _conn),
        ):
            warmup._build_entity_context("Did Bob update the Example plan?", tenant_id=TENANT)
        assert cur.statements
        for statement in cur.statements:
            assert TENANT in statement

    def test_a_failed_entity_lookup_is_logged_at_warning(
        self, db: FakeDB, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with (
            patch(
                "robothor.engine.warmup._build_entity_context",
                side_effect=IndexError("tuple index out of range"),
            ),
            caplog.at_level(logging.WARNING, logger="robothor.engine.warmup"),
        ):
            build_interactive_preamble("main", MESSAGE, tenant_id=TENANT)
        assert any(
            r.levelno == logging.WARNING and "entity context" in r.getMessage()
            for r in caplog.records
        )


@pytest.mark.integration
class TestEntitySqlMogrify:
    """The same statements through a real psycopg2 cursor's ``mogrify``."""

    @pytest.mark.parametrize("v2", ["0", "1"])
    def test_mogrify_accepts_every_entity_statement(self, v2: str) -> None:
        from robothor.db import get_connection as real_get_connection

        with real_get_connection() as real:
            real_cur = real.cursor()
            mogrified: list[bytes] = []

            class _Cur:
                def execute(self, sql: str, params: Any = ()) -> None:
                    mogrified.append(real_cur.mogrify(sql, params))

                def fetchall(self) -> list[Any]:
                    return []

            class _Conn:
                def cursor(self, cursor_factory: Any = None) -> _Cur:
                    return _Cur()

            @contextmanager
            def _conn(*a: Any, **kw: Any):
                yield _Conn()

            with (
                patch.dict(os.environ, {"MEMORY_WARMUP_RECALL_V2": v2}),
                patch("robothor.db.get_connection", _conn),
            ):
                warmup._build_entity_context("Did Bob update the Example plan?", tenant_id=TENANT)
        assert mogrified


class TestThereIsNoSwitch:
    """The parallel warmup is the only warmup: no setting turns it off."""

    def test_feature_flags_has_no_warmup_reader(self) -> None:
        import inspect

        from robothor.engine import feature_flags

        assert not [n for n in dir(feature_flags) if "warmup_parallel" in n]
        assert "ROBOTHOR_WARMUP_PARALLEL" not in inspect.getsource(feature_flags)

    def test_no_module_reads_the_old_variable(self) -> None:
        import inspect

        from robothor.engine import runner
        from robothor.settings import model

        for module in (warmup, runner, model, enrichment):
            assert "ROBOTHOR_WARMUP_PARALLEL" not in inspect.getsource(module), module

    def test_the_old_variable_set_to_zero_changes_nothing(self, db: FakeDB) -> None:
        os.environ["ROBOTHOR_WARMUP_PARALLEL"] = "0"
        timings: dict[str, float] = {}
        _build(fan_out=False, timings=timings)
        warmup.wait_for_deferred_writes(timeout=2.0)
        block_reads = [
            s
            for s in db.statements
            if "agent_memory_blocks" in s and ("RETURNING" in s or s.upper().startswith("SELECT"))
        ]
        assert len(block_reads) == 1, block_reads
        assert db.count("memory_facts") == 1
        assert len(timings) == 8
        enrichment._cache.clear()
        warmup.enrich_identity_for_turn("main", IDENTITY)
        assert db.count("crm_people") == 1
