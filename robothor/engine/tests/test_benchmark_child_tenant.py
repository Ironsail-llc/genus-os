"""Every benchmark task run is scoped to the sandbox tenant, and names its parent.

Incident, 2026-09-13 audit of the 04:00 fleet benchmark: of 78 task runs
spawned by ``benchmark-runner``, **75 recorded**
``agent_runs.tenant_id = '<the production tenant>'``. Only the three runs of
the one suite that declares fixtures (``agent-architect``) got
``benchmark-sandbox``.

The cause was a single ``if`` in the harness. ``_seed_task_fixtures`` returns
``None`` for a suite that declares neither ``fixtures:`` nor ``state_checks``,
and ``_execute_task_run`` read that ``None`` as "run wherever the agent
normally runs" — so the sandbox tenant was reached only as a side effect of
seeding, never as a property of being a benchmark run.

What that cost, with the D2 write boundary already in place:

* 60 writes (``log_fact_access``) were refused by ``benchmark_write_refused``.
  Nothing leaked — but the deny-list plus that boundary were the *only* things
  standing between fixture fiction and production, and a boundary is not a
  tenant assignment.
* Production-tenant DAL lookups ran with fixture identifiers:
  ``get_person {"id": "bob.quill@example.com"}`` reached Postgres as a uuid
  comparison and raised ``InvalidTextRepresentation``.
* Every task run recorded ``parent_run_id = NULL``, so "which runs belonged to
  last night's benchmark?" could only be answered by a time window.

These tests pin the fix: the sandbox tenant is a property of the HARNESS, not
of the suite's fixture declaration, and every child is linked to the run that
spawned it. The one thing that must NOT move is ``benchmark_results`` — that
row is the grade ledger, and a grade written into a tenant teardown sweeps is
a grade that does not survive the night.

The "Fix round 1" section below covers the hostile review of the first draft:
the escape hatch for suites that genuinely have to read production
(``execution_tenant``), suite-level tenant resolution, the advisory lock that
serialises the shared sandbox, and the untracked-parent case that ungating
lineage made reachable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import TriggerType
from robothor.engine.tools.dispatch import ToolContext

SANDBOX = "benchmark-sandbox"
OWNING_TENANT = "acme-instance"
PARENT_RUN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

CTX = ToolContext(
    agent_id="benchmark-runner",
    run_id=PARENT_RUN_ID,
    workspace="/tmp/test-workspace",
)


# ─── Helpers (mirrors test_benchmark_decontamination.py) ─────────────────────


def _mock_blocks() -> tuple[dict[str, str], Any, Any]:
    store: dict[str, str] = {}

    def read_block(name: str) -> dict[str, Any]:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-08-21T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict[str, Any]:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _make_mock_run() -> MagicMock:
    run = MagicMock()
    run.output_text = "ok"
    run.total_cost_usd = 0.01
    run.steps = [MagicMock()]
    run.status = MagicMock(value="completed")
    run.id = "child-run-1"
    run.error_message = None
    return run


def _suite(
    *,
    fixtures: bool = False,
    execution_tenant: str | None = None,
    tasks: int = 1,
    task_postures: list[str | None] | None = None,
) -> str:
    """A suite blob for the harness.

    ``task_postures`` declares the posture PER TASK, which is the shape every
    shipped opt-out actually has: ``main``'s ``memory-recall`` sits beside the
    six ``_honesty`` cases, and ``agent-architect`` and ``curiosity-engine``
    each keep fixture-bearing tasks next to theirs. A uniform suite cannot
    exercise the per-task branch at all — with every task opted out,
    ``_suite_execution_tenant`` returns None and the branch is right by
    accident.
    """

    def _task(index: int) -> dict[str, Any]:
        return {
            "id": f"t{index}",
            "prompt": "x",
            "category": "correctness",
            "weight": 1.0,
            "expected": {"must_contain": ["ok"]},
        }

    task_list = [_task(i + 1) for i in range(tasks)]
    if task_postures is not None:
        assert len(task_postures) == len(task_list)
        for task, posture in zip(task_list, task_postures, strict=True):
            if posture is not None:
                task["execution_tenant"] = posture
    suite: dict[str, Any] = {
        "id": "s1",
        "agent_id": "email-analyst",
        "max_cost_usd": 100.0,
        "tasks": task_list,
    }
    if execution_tenant is not None:
        suite["execution_tenant"] = execution_tenant
    if fixtures:
        for task in task_list:
            task["fixtures"] = ["a_person"]
        suite["fixtures"] = {
            "fixtures": {
                "a_person": {"table": "crm_people", "values": {"email": "alice@example.com"}}
            }
        }
    return json.dumps(suite)


#: A manifest's real-ish grant. A child whose ``tools_allowed`` is one harmless
#: tool makes every deny-list assertion vacuous — the deny-list is computed as
#: ``tools_allowed - benchmark_allowed_tools(...)``.
WIDE_TOOLS_ALLOWED = [
    "exec",
    "read_file",
    "write_file",
    "list_people",
    "create_person",
    "update_person",
    "delete_person",
    "create_task",
    "update_task",
    "resolve_task",
    "store_memory",
    "append_to_block",
    "send_notification",
]


def _child_config() -> MagicMock:
    cfg = MagicMock()
    cfg.max_iterations = 10
    cfg.tools_allowed = list(WIDE_TOOLS_ALLOWED)
    cfg.tools_denied = []
    cfg.is_benchmark = False
    cfg.model_primary = "openrouter/test/model"
    return cfg


class _Recorder:
    """A fake runner that records what each child run was handed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.config = MagicMock()
        self.config.manifest_dir = Path("/tmp")

    async def execute(self, **kwargs: Any) -> MagicMock:
        from robothor.db.connection import effective_tenant
        from robothor.engine.run_context import in_benchmark_run

        self.calls.append(
            {
                **kwargs,
                "_bound_tenant": effective_tenant(),
                "_marker": in_benchmark_run(),
            }
        )
        return _make_mock_run()


async def _run_suite(
    runner: Any,
    *,
    sandbox_on: bool,
    fixtures: bool = False,
    seeded_tenant: str = SANDBOX,
    teardowns: list[str] | None = None,
    ensured: list[str] | None = None,
    execution_tenant: str | None = None,
    tasks: int = 1,
    task_postures: list[str | None] | None = None,
    seed_raises: bool = False,
    ensure_raises: bool = False,
    lock_state: set[str] | None = None,
    lock_unavailable: bool = False,
    expect_error: bool = False,
) -> dict[str, Any]:
    """Drive ``_benchmark_run`` end to end with the database stubbed out."""
    from contextlib import contextmanager

    from robothor.engine.benchmark_sandbox import SeededFixtures
    from robothor.engine.tools.handlers import benchmark as bench

    store, read_fn, write_fn = _mock_blocks()
    store["benchmark:email-analyst:s1"] = _suite(
        fixtures=fixtures,
        execution_tenant=execution_tenant,
        tasks=tasks,
        task_postures=task_postures,
    )

    def _ensure(tenant_id: str | None = None) -> str:
        if ensure_raises:
            raise RuntimeError("crm_tenants is unreachable")
        if ensured is not None:
            ensured.append(tenant_id or seeded_tenant)
        return tenant_id or seeded_tenant

    def _seed(spec: Any, keys: Any, tenant_id: str | None = None) -> SeededFixtures:
        if seed_raises:
            raise RuntimeError("insert_row failed halfway")
        return SeededFixtures(tenant_id=tenant_id or seeded_tenant)

    def _teardown(tenant_id: str | None = None) -> int:
        if teardowns is not None:
            teardowns.append(tenant_id or seeded_tenant)
        return 0

    @contextmanager
    def _lock(tenant_id: str) -> Any:
        """A fake of the Postgres advisory lock: one holder per tenant."""
        from robothor.engine.benchmark_sandbox import (
            LOCK_ACQUIRED,
            LOCK_HELD_ELSEWHERE,
            LOCK_UNAVAILABLE,
        )

        if lock_unavailable:
            yield LOCK_UNAVAILABLE
            return
        if lock_state is None:
            yield LOCK_ACQUIRED
            return
        if tenant_id in lock_state:
            yield LOCK_HELD_ELSEWHERE
            return
        lock_state.add(tenant_id)
        try:
            yield LOCK_ACQUIRED
        finally:
            lock_state.discard(tenant_id)

    import robothor.engine.benchmark_sandbox as bs

    with (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=runner),
        # A FRESH config per task: `_shape_child_config` mutates in place, so a
        # single shared mock makes every recorded call show the LAST task's
        # deny-list — silently turning a per-task assertion into a whole-suite one.
        patch(
            "robothor.engine.config.load_agent_config",
            side_effect=lambda *_a, **_kw: _child_config(),
        ),
        patch.object(bench, "sandbox_active", return_value=sandbox_on),
        patch.object(bs, "ensure_sandbox_tenant", side_effect=_ensure),
        patch.object(bs, "seed_fixtures", side_effect=_seed),
        patch.object(bs, "teardown_sandbox", side_effect=_teardown),
        patch.object(bs, "sandbox_suite_lock", _lock),
        patch.object(bench, "_write_benchmark_result_row"),
    ):
        result = await bench._benchmark_run(
            {"agent_id": "email-analyst", "suite_id": "s1", "tag": "t"}, CTX
        )
    if not expect_error:
        assert not result.get("error"), result
    return cast("dict[str, Any]", result)


@pytest.fixture(autouse=True)
def _owning_tenant(monkeypatch: Any) -> None:
    """The instance's own tenant, so a leak into it is visible by name."""
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", OWNING_TENANT)
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_TENANT", SANDBOX)


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: Any) -> None:
    """Nothing in this file may reach a real database.

    Added after CI caught what this box could not: the grade-ledger test drove
    the real ``sandbox_suite_lock``, which takes a connection. On a runner with
    no Postgres the lock correctly refused the suite and the test failed for a
    reason unrelated to what it was testing; here it passed, because this box
    happens to have a live database. A test whose result depends on that is not
    measuring what it claims to.

    Every database door this file needs is stubbed per test. This shuts the
    rest, loudly, so the next one is a red test rather than a green one that
    was only ever green on one machine.
    """

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "a test in test_benchmark_child_tenant.py reached the database — "
            "stub the path it took instead of depending on a live Postgres"
        )

    monkeypatch.setattr("robothor.db.connection.get_connection", _refuse)


# ─── (1) the sandbox tenant is a property of the harness ─────────────────────


class TestEveryTaskRunIsScopedToTheSandbox:
    @pytest.mark.asyncio
    async def test_a_suite_without_fixtures_still_runs_in_the_sandbox(self) -> None:
        """The 75-of-78 case. No ``fixtures:``, no ``state_checks:`` — and the
        child must STILL execute as the sandbox tenant."""
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True)

        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert call.get("tenant_id") == SANDBOX, (
            "a fixture-less benchmark task ran under the graded agent's own "
            "tenant — this is the defect that put 75 runs in production"
        )
        assert call["_bound_tenant"] == SANDBOX, (
            "runner.execute got the sandbox tenant but the connection binding "
            "did not — RLS refuses the sandbox INSERT and reads see production"
        )
        assert call["trigger_type"] is TriggerType.SUB_AGENT

    @pytest.mark.asyncio
    async def test_the_sandbox_tenant_row_is_ensured_first(self) -> None:
        """A fixture-less suite never called ``ensure_sandbox_tenant``; now the
        tenant has to exist before the child writes anything FK-constrained."""
        ensured: list[str] = []
        await _run_suite(_Recorder(), sandbox_on=True, ensured=ensured)
        assert SANDBOX in ensured

    @pytest.mark.asyncio
    async def test_a_fixture_bearing_suite_is_unchanged(self) -> None:
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True, fixtures=True)
        call = runner.calls[0]
        assert call.get("tenant_id") == SANDBOX
        assert call["_bound_tenant"] == SANDBOX

    @pytest.mark.asyncio
    async def test_sandbox_off_keeps_todays_behaviour(self) -> None:
        """With the sandbox off nothing is re-pointed — but the write boundary
        from D2 is still armed, which is what makes ``off`` survivable."""
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=False)
        call = runner.calls[0]
        assert call.get("tenant_id") is None
        assert call["_bound_tenant"] == OWNING_TENANT
        assert call["_marker"] is True, "the benchmark write boundary was not armed"

    @pytest.mark.asyncio
    async def test_the_write_boundary_is_armed_inside_the_sandbox_too(self) -> None:
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True)
        assert runner.calls[0]["_marker"] is True


# ─── (2) lineage: a task run names the run that spawned it ───────────────────


class TestLineage:
    @pytest.mark.asyncio
    async def test_every_task_run_records_the_benchmark_runner_as_its_parent(self) -> None:
        """No ``parent_run_id`` meant the audit had to go by time window.

        Deliberately sets no decontamination flag: lineage is not a rollout,
        it is the only way to ask "which runs were last night's benchmark?".
        """
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True)
        spawn_context = runner.calls[0].get("spawn_context")
        assert spawn_context is not None, "benchmark task run spawned with no parent linkage"
        assert spawn_context.parent_run_id == PARENT_RUN_ID

    @pytest.mark.asyncio
    async def test_lineage_holds_with_the_sandbox_off(self) -> None:
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=False)
        spawn_context = runner.calls[0].get("spawn_context")
        assert spawn_context is not None
        assert spawn_context.parent_run_id == PARENT_RUN_ID


# ─── (3) teardown still empties the sandbox ──────────────────────────────────


class TestTeardown:
    @pytest.mark.asyncio
    async def test_a_fixtureless_sandbox_task_is_torn_down(self) -> None:
        """A fixture-less task can now WRITE the sandbox (CRM writes are
        re-allowed there), so it needs the same sweep a seeded one gets."""
        teardowns: list[str] = []
        await _run_suite(_Recorder(), sandbox_on=True, teardowns=teardowns)
        assert teardowns == [SANDBOX]

    @pytest.mark.asyncio
    async def test_a_seeded_task_is_torn_down(self) -> None:
        teardowns: list[str] = []
        await _run_suite(_Recorder(), sandbox_on=True, fixtures=True, teardowns=teardowns)
        assert teardowns == [SANDBOX]

    @pytest.mark.asyncio
    async def test_nothing_is_swept_with_the_sandbox_off(self) -> None:
        teardowns: list[str] = []
        await _run_suite(_Recorder(), sandbox_on=False, teardowns=teardowns)
        assert teardowns == []


# ─── (4) the grade ledger keeps the OWNING tenant ────────────────────────────


class TestTheGradeLedgerIsNotAgentData:
    @pytest.mark.asyncio
    async def test_the_results_row_is_written_outside_the_sandbox_scope(self) -> None:
        """``benchmark_results`` is the grade ledger. Mislabelling it
        ``benchmark-sandbox`` would hide every score from the fleet goal
        metric AND put it in a tenant teardown sweeps.

        Every database door is shut, including the sandbox lock's. This test
        drove the REAL ``sandbox_suite_lock`` until CI caught it: on a runner
        with no Postgres the lock (rightly) refuses the suite, the suite never
        reaches its result row, and the assertion failed for a reason that had
        nothing to do with tenants. It passed here only because this box has a
        live database — an accident, and exactly the kind of hidden dependency
        that makes a green suite mean less than it looks.
        """
        from contextlib import contextmanager

        from robothor.db.connection import effective_tenant
        from robothor.engine.tools.handlers import benchmark as bench

        seen: list[str] = []

        def _record(**_kwargs: Any) -> None:
            seen.append(effective_tenant())

        import robothor.engine.benchmark_sandbox as bs

        @contextmanager
        def _lock(_tenant_id: str) -> Any:
            yield bs.LOCK_ACQUIRED

        runner = _Recorder()
        store, read_fn, write_fn = _mock_blocks()
        store["benchmark:email-analyst:s1"] = _suite()

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=runner),
            patch("robothor.engine.config.load_agent_config", return_value=_child_config()),
            patch.object(bench, "sandbox_active", return_value=True),
            patch.object(bs, "ensure_sandbox_tenant", return_value=SANDBOX),
            patch.object(bs, "teardown_sandbox", return_value=0),
            patch.object(bs, "sandbox_suite_lock", _lock),
            patch.object(bench, "_write_benchmark_result_row", side_effect=_record),
        ):
            result = await bench._benchmark_run(
                {"agent_id": "email-analyst", "suite_id": "s1", "tag": "t"}, CTX
            )

        assert not result.get("error"), result
        assert seen == [OWNING_TENANT], (
            "the benchmark_results row was written inside the sandbox tenant "
            "scope — it must not land in a tenant teardown sweeps"
        )

    def test_the_results_insert_still_takes_the_tenant_default(self) -> None:
        """The INSERT deliberately omits ``tenant_id``.

        This pins ONLY that the sandbox never reaches the row. It does not
        pin which tenant the row lands in: migration 098 deliberately left
        this writer's column DEFAULT at ``'robothor-primary'`` — the FIRST
        instance's id, not "the owning tenant" — and says to retarget it in a
        follow-up once the writer passes a tenant explicitly. Until then the
        grade ledger is mislabelled on any other install, and this test would
        still pass. See M1 in the review.
        """
        import robothor.engine.tools.handlers.benchmark as bench

        src = Path(bench.__file__).read_text()
        start = src.index("INSERT INTO benchmark_results")
        insert = src[start : src.index('"""', start)]
        assert "tenant_id" not in insert
        assert "sandbox" not in insert.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Fix round 1 — hostile review of c9cc402d6f
# ═══════════════════════════════════════════════════════════════════════════


# ─── C1: a suite may opt out of the sandbox for its READS ────────────────────


class TestExecutionTenantPosture:
    """Not every suite can be graded in an empty tenant.

    The first audit of this change scanned keyword lists for numeric literals
    and concluded that no suite reads production data. It missed judge rubrics
    and tenant-scoped tool reads, which is where the dependency actually lives:
    ``main::memory-recall`` is graded ``must_not_contain: ["no information|
    don't know|cannot find"]`` over a ``search_memory`` that is tenant-scoped,
    so in an empty sandbox the honest answer scores 0 and the agent is punished
    for not fabricating.

    ``execution_tenant: production-read-only`` is the escape hatch: the child
    runs under the OWNING tenant with the deny-list and the write boundary
    armed exactly as they are when the sandbox is off, so its reads see real
    data and none of its writes can land.
    """

    @pytest.mark.asyncio
    async def test_default_is_the_sandbox(self) -> None:
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True)
        assert runner.calls[0].get("tenant_id") == SANDBOX

    @pytest.mark.asyncio
    async def test_production_read_only_runs_under_the_owning_tenant(self) -> None:
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True, execution_tenant="production-read-only")
        call = runner.calls[0]
        assert call.get("tenant_id") is None, (
            "a production-read-only suite was re-pointed at the sandbox anyway"
        )
        assert call["_bound_tenant"] == OWNING_TENANT

    @pytest.mark.asyncio
    async def test_production_read_only_still_refuses_every_write(self) -> None:
        """The whole safety of the escape hatch. Read-only is the *point*."""
        runner = _Recorder()
        await _run_suite(runner, sandbox_on=True, execution_tenant="production-read-only")
        call = runner.calls[0]
        assert call["_marker"] is True, "the benchmark write boundary was not armed"
        assert call["agent_config"].is_benchmark is True
        denied = set(call["agent_config"].tools_denied)
        for tool in ("store_memory", "create_person", "update_person", "create_task", "exec"):
            assert tool in denied, f"{tool} was writable in a production-read-only run"

    @pytest.mark.asyncio
    async def test_production_read_only_seeds_and_sweeps_nothing(self) -> None:
        teardowns: list[str] = []
        ensured: list[str] = []
        await _run_suite(
            _Recorder(),
            sandbox_on=True,
            execution_tenant="production-read-only",
            teardowns=teardowns,
            ensured=ensured,
        )
        assert teardowns == []
        assert ensured == []

    @pytest.mark.asyncio
    async def test_an_unknown_posture_fails_the_suite_closed(self) -> None:
        """A typo must not silently grade the fleet against production."""
        from robothor.engine.tools.handlers import benchmark as bench

        result = await _run_suite(
            _Recorder(), sandbox_on=True, execution_tenant="produciton", expect_error=True
        )
        assert result.get("success") is False
        assert "execution_tenant" in str(result.get("error", ""))
        assert bench  # imported for the failure message's sake

    @pytest.mark.asyncio
    async def test_the_harness_says_which_tenant_each_task_ran_under(self, caplog: Any) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="robothor.engine.tools.handlers.benchmark"):
            await _run_suite(_Recorder(), sandbox_on=True)
        assert any(SANDBOX in r.getMessage() for r in caplog.records), (
            "nothing in the log says which tenant the task ran under"
        )


# ─── I1: an untracked parent must not be papered over with ctx.run_id ────────


class TestAmbientUntrackedParent:
    """``runner.py`` sets ``parent_run_id=""`` when the parent's own row was
    refused (``tracking_disabled``). Overriding that with ``ctx.run_id`` hands
    every child a dangling FK: each child's ``create_run`` then fails the same
    way, sets ``tracking_disabled`` and pages the operator — 78 alerts and a
    night with no run rows. The decontamination gate used to make this branch
    unreachable; ungating lineage made it live."""

    def test_an_empty_ambient_parent_is_not_replaced_by_the_caller_run(self) -> None:
        from robothor.engine.models import SpawnContext
        from robothor.engine.tools.handlers import spawn as spawn_mod
        from robothor.engine.tools.handlers.benchmark import _benchmark_spawn_context

        token = spawn_mod._current_spawn_context.set(
            SpawnContext(
                parent_run_id="",
                parent_agent_id="p",
                correlation_id="c",
                nesting_depth=0,
            )
        )
        try:
            built = _benchmark_spawn_context(CTX)
        finally:
            spawn_mod._current_spawn_context.reset(token)

        assert built is None or built.parent_run_id == "", (
            "an untracked parent was given a dangling parent_run_id from ctx"
        )

    def test_no_ambient_context_still_falls_back_to_the_calling_run(self) -> None:
        from robothor.engine.tools.handlers.benchmark import _benchmark_spawn_context

        built = _benchmark_spawn_context(CTX)
        assert built is not None
        assert built.parent_run_id == PARENT_RUN_ID

    def test_nesting_depth_is_not_incremented_twice(self) -> None:
        """``runner.py:659`` adds one of its own."""
        from robothor.engine.tools.handlers.benchmark import _benchmark_spawn_context

        built = _benchmark_spawn_context(CTX)
        assert built is not None
        assert built.nesting_depth == 0


# ─── I2: the tenant is resolved before anything is seeded ────────────────────


class TestTenantResolvedBeforeSeeding:
    @pytest.mark.asyncio
    async def test_a_mid_seed_failure_is_still_swept(self) -> None:
        """Partial fixture rows must not become tomorrow's ambient state."""
        runner = _Recorder()
        teardowns: list[str] = []
        await _run_suite(
            runner, sandbox_on=True, fixtures=True, teardowns=teardowns, seed_raises=True
        )
        assert runner.calls == [], "a child ran even though seeding failed"
        assert teardowns == [SANDBOX], "a half-seeded sandbox was left standing"


# ─── I3: the tenant is resolved ONCE per suite, and a fault is not a grade ───


class TestSuiteLevelTenantResolution:
    @pytest.mark.asyncio
    async def test_the_tenant_is_ensured_once_per_suite_not_once_per_task(self) -> None:
        ensured: list[str] = []
        await _run_suite(_Recorder(), sandbox_on=True, ensured=ensured, tasks=4)
        assert ensured == [SANDBOX], f"ensure_sandbox_tenant ran {len(ensured)} times for 4 tasks"

    @pytest.mark.asyncio
    async def test_an_infra_fault_fails_the_suite_instead_of_grading_it_zero(self) -> None:
        """A lock, a connection blip or an unapplied migration 104 must not be
        indistinguishable from an agent that failed every case — that number
        feeds benchmark_pass_rate and the self-improvement triage."""
        runner = _Recorder()
        result = await _run_suite(runner, sandbox_on=True, ensure_raises=True, expect_error=True)
        assert result.get("success") is False
        assert "sandbox" in str(result.get("error", "")).lower()
        assert result.get("aggregate_score") is None, "an infra fault was reported as a grade"
        assert runner.calls == []


# ─── I4: one suite at a time may hold the shared sandbox tenant ──────────────


class TestSandboxSuiteLock:
    """``teardown_sandbox`` hard-deletes every row of 14 tables in the tenant,
    whoever wrote them. Two callers can be inside ``_benchmark_run`` at once in
    one daemon — the 04:00 fleet cron and auto-researcher's before/after
    measurement — and the loser grades against a CRM someone else emptied
    mid-task. Nothing serialised this."""

    @pytest.mark.asyncio
    async def test_a_second_concurrent_suite_is_refused(self) -> None:
        import asyncio

        held: list[str] = []
        gate = asyncio.Event()

        class _SlowRunner(_Recorder):
            async def execute(self, **kwargs: Any) -> Any:
                held.append("in")
                await gate.wait()
                return await super().execute(**kwargs)

        first_runner = _SlowRunner()
        second_runner = _Recorder()
        lock_state: set[str] = set()

        first = asyncio.create_task(
            _run_suite(first_runner, sandbox_on=True, lock_state=lock_state)
        )
        # Bounded, not a bare spin: if the first suite never reaches its
        # runner this must fail with a message, not hang the suite.
        for _ in range(1000):
            if held:
                break
            await asyncio.sleep(0.001)
        assert held, "the first suite never started — it cannot hold the lock"
        second = await _run_suite(
            second_runner, sandbox_on=True, lock_state=lock_state, expect_error=True
        )
        gate.set()
        await first

        from robothor.engine.benchmark_sandbox import LOCK_HELD_ELSEWHERE

        assert second.get("success") is False
        assert second.get("reason") == LOCK_HELD_ELSEWHERE
        assert "another benchmark suite" in str(second.get("error", "")).lower()
        assert second_runner.calls == [], "the refused suite still ran tasks"

    @pytest.mark.asyncio
    async def test_an_unavailable_lock_refuses_the_suite(self) -> None:
        """Fail CLOSED. An earlier draft ran the suite unserialised with an
        ERROR in the log, on the argument that a dark fleet night is worse.
        It is not: an unserialised suite can have its fixtures swept mid-task
        by a concurrent one and the result is a plausible low score with no
        error anywhere — a wrong number nobody can tell is wrong. A refused
        suite is a visible absence. Benchmark safety fails closed."""
        from robothor.engine.benchmark_sandbox import LOCK_UNAVAILABLE

        runner = _Recorder()
        teardowns: list[str] = []
        result = await _run_suite(
            runner,
            sandbox_on=True,
            lock_unavailable=True,
            teardowns=teardowns,
            expect_error=True,
        )

        assert result.get("success") is False
        assert result.get("reason") == LOCK_UNAVAILABLE
        assert result.get("aggregate_score") is None, "an unlocked suite was still graded"
        assert runner.calls == [], "the suite ran without holding the sandbox lock"
        assert teardowns == [], "a refused suite swept a tenant it never held"

    @pytest.mark.asyncio
    async def test_an_unavailable_lock_does_not_block_a_sandbox_off_run(self) -> None:
        """With the sandbox off there is no shared tenant to serialise."""
        runner = _Recorder()
        result = await _run_suite(runner, sandbox_on=False, lock_unavailable=True)
        assert result.get("success") is True
        assert len(runner.calls) == 1

    @pytest.mark.asyncio
    async def test_the_lock_is_released_when_the_suite_ends(self) -> None:
        lock_state: set[str] = set()
        await _run_suite(_Recorder(), sandbox_on=True, lock_state=lock_state)
        assert lock_state == set(), "the sandbox lock outlived its suite"

    @pytest.mark.asyncio
    async def test_no_lock_is_taken_when_the_sandbox_is_off(self) -> None:
        lock_state: set[str] = set()
        await _run_suite(_Recorder(), sandbox_on=False, lock_state=lock_state)
        assert lock_state == set()


# ─── C1: the shipped suites, and what the audit concluded about each ─────────


class TestShippedSuitePostures:
    """The first audit of this change was wrong, and its conclusion went into
    platform docs. It scanned keyword lists for numeric literals and missed the
    two places the dependency actually lives: LLM **judge rubrics**, and tools
    that read tenant-scoped data (``search_memory``, ``get_agent_stats``,
    ``get_knowledge_gaps``, the tenant-scoped ``agent_memory_blocks``).

    These pin the re-audit's conclusions against the files themselves, so a
    later edit that drops a posture — or adds a task that needs one — goes red
    here instead of on the fleet's grade the next morning.
    """

    BENCH = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"

    #: task id -> the tenant-scoped read that makes an empty sandbox wrong.
    NEEDS_PRODUCTION_READS: dict[str, str] = {
        "memory-recall": "search_memory over tenant-scoped memory_facts",
        "fleet-analysis": "get_agent_stats + tenant-scoped memory blocks",
        "cross-pollination": "the autoagent_learnings memory block",
        "basic-gap-analysis": "analyze_knowledge_gaps over memory_entities",
        "efficiency-completion": "analyze_knowledge_gaps over memory_entities",
        "dedup-prior-findings": "the prior-findings memory block",
        "safety-store-concrete": "analyze_knowledge_gaps over memory_entities",
    }

    @classmethod
    def _all_tasks(cls) -> dict[str, dict[str, Any]]:
        import yaml as _yaml

        tasks: dict[str, dict[str, Any]] = {}
        for path in sorted(cls.BENCH.glob("*/suite.yaml")):
            suite = _yaml.safe_load(path.read_text()) or {}
            if suite.get("runner"):  # native suites bring their own tenant
                continue
            for task in suite.get("tasks") or []:
                tasks[str(task["id"])] = {**task, "_suite": suite, "_path": path}
        return tasks

    def test_every_audited_task_declares_the_posture(self) -> None:
        from robothor.engine.tools.handlers.benchmark import (
            PRODUCTION_READ_ONLY_POSTURE,
            _execution_posture,
        )

        tasks = self._all_tasks()
        for task_id, why in self.NEEDS_PRODUCTION_READS.items():
            assert task_id in tasks, f"{task_id} has vanished — re-run the audit"
            task = tasks[task_id]
            posture = _execution_posture(task, task["_suite"])
            assert posture == PRODUCTION_READ_ONLY_POSTURE, (
                f"{task['_path'].parent.name}::{task_id} grades an agent on {why}; "
                "in an empty sandbox the honest answer fails"
            )

    def test_no_other_task_opts_out(self) -> None:
        """Every opt-out is a task NOT protected by the sandbox, so the list has
        to stay short and deliberate rather than becoming the default."""
        from robothor.engine.tools.handlers.benchmark import (
            PRODUCTION_READ_ONLY_POSTURE,
            _execution_posture,
        )

        opted_out = {
            task_id
            for task_id, task in self._all_tasks().items()
            if _execution_posture(task, task["_suite"]) == PRODUCTION_READ_ONLY_POSTURE
        }
        assert opted_out == set(self.NEEDS_PRODUCTION_READS), (
            "a suite opted out of the sandbox without an entry here: "
            f"{sorted(opted_out ^ set(self.NEEDS_PRODUCTION_READS))}"
        )

    #: Tasks whose SCORE does not move in an empty tenant, but whose CASE stops
    #: measuring anything: the OR-list already contains the "nothing found"
    #: branch, or the assertion is satisfied by the agent's own name or a
    #: literal 0. They keep the sandbox — a vacuous pass is not worth handing a
    #: graded child the production tenant for — but they are recorded, because
    #: "the score did not move" is exactly why nobody would notice.
    #:
    #: The runbook's "any task that moved is a finding" cannot surface these.
    #: This dict is the only place they exist.
    GOES_VACUOUS_IN_AN_EMPTY_TENANT: dict[str, str] = {
        "enrich-existing-contact": (
            "'Pick the top 1 least-complete contact from the CRM' against an empty "
            "CRM; passes on `no.*found|not found|nothing to enrich`"
        ),
        "output-format-one-line": (
            "'Enrich any contact with missing fields'; the one-line format is never "
            "exercised because there is no contact to enrich"
        ),
        "rag-first-not-subagent": (
            "'Enrich a contact'; passes on the not-found branch without ever "
            "demonstrating the RAG-before-spawn ordering the case exists to grade"
        ),
        "workflow-completes": (
            "'fetch contacts, run pairwise similarity'; zero contacts means the "
            "dedup workflow is never run, and `no duplicates|nothing to merge` passes"
        ),
        "status-file-written": "same empty dedup workflow; the status file says nothing happened",
        "write-file-for-status": "same empty dedup workflow; write_file-vs-exec is still graded",
        "review-learnings": (
            "reads the tenant-scoped autoresearch_learnings block — the same class as "
            "cross-pollination — but passes because its OR-list contains the agent's "
            "own name"
        ),
        "list-tasks": "list_my_tasks returns nothing; `0` is in the OR-list",
    }

    def test_the_vacuous_tasks_are_declared_and_still_sandboxed(self) -> None:
        """They are a known cost of the sandbox, not an oversight.

        Kept on `sandbox` deliberately: handing a graded child the production
        tenant to rescue a case that only ever asserted "nothing found" would
        trade real isolation for a number that was never measuring much.
        """
        from robothor.engine.tools.handlers.benchmark import (
            SANDBOX_POSTURE,
            _execution_posture,
        )

        tasks = self._all_tasks()
        for task_id, why in self.GOES_VACUOUS_IN_AN_EMPTY_TENANT.items():
            assert task_id in tasks, f"{task_id} has vanished — re-run the audit ({why})"
            task = tasks[task_id]
            assert _execution_posture(task, task["_suite"]) == SANDBOX_POSTURE, (
                f"{task_id} is recorded as going vacuous AND opts out of the "
                "sandbox — one of the two is wrong"
            )

    def test_the_two_declared_sets_do_not_overlap(self) -> None:
        """A task either scores 0 in an empty tenant or passes vacuously in one.
        Both would mean the audit contradicts itself."""
        overlap = set(self.NEEDS_PRODUCTION_READS) & set(self.GOES_VACUOUS_IN_AN_EMPTY_TENANT)
        assert not overlap, f"declared as both breaking and vacuous: {sorted(overlap)}"

    def test_every_shipped_posture_is_valid(self) -> None:
        """A typo in a suite file must be caught here, not at 04:00."""
        from robothor.engine.tools.handlers.benchmark import _execution_posture

        for task in self._all_tasks().values():
            _execution_posture(task, task["_suite"])  # raises ValueError on a typo

    def test_a_task_that_seeds_fixtures_never_opts_out(self) -> None:
        """Hard rule. `production-read-only` seeds nothing, so a fixture-bearing
        task that opted out would interpolate `{{fixture.…}}` into its own
        prompt and grade the agent on a record that does not exist."""
        from robothor.engine.tools.handlers.benchmark import (
            PRODUCTION_READ_ONLY_POSTURE,
            _execution_posture,
        )

        for task_id, task in self._all_tasks().items():
            if not task.get("fixtures"):
                continue
            assert _execution_posture(task, task["_suite"]) != PRODUCTION_READ_ONLY_POSTURE, (
                f"{task_id} seeds fixtures but opts out of the tenant they are seeded in"
            )

    #: Opted-out tasks whose ``state_checks`` therefore do not run, and why that
    #: is the better trade. Not a hard rule — a declared one, because the
    #: alternative to naming them is not noticing.
    STATE_CHECKS_GO_INERT: dict[str, str] = {
        "cross-pollination": (
            "its rubric needs the autoagent_learnings block (tenant-scoped, empty in "
            "the sandbox) AND a created CRM task. Neither posture satisfies both; "
            "production-read-only reproduces today's grade exactly, where the sandbox "
            "posture would change it for a reason nobody chose."
        ),
    }

    def test_an_opted_out_state_check_is_declared(self) -> None:
        from robothor.engine.tools.handlers.benchmark import (
            PRODUCTION_READ_ONLY_POSTURE,
            _execution_posture,
        )

        inert = {
            task_id
            for task_id, task in self._all_tasks().items()
            if (task.get("expected") or {}).get("state_checks")
            and _execution_posture(task, task["_suite"]) == PRODUCTION_READ_ONLY_POSTURE
        }
        assert inert == set(self.STATE_CHECKS_GO_INERT), (
            "a task's state_checks silently stopped running because it opted out "
            f"of the sandbox: {sorted(inert ^ set(self.STATE_CHECKS_GO_INERT))}"
        )

    def test_the_honesty_cases_pin_the_sandbox(self) -> None:
        """Their premise is that the record is ABSENT. Pinned in the file that
        owns them so a future suite-level opt-out cannot drag them into a
        tenant where `bob.quill@example.com` might actually exist — on this
        instance that fixture really did reach the production CRM."""
        import yaml as _yaml

        from robothor.engine.tools.handlers.benchmark import SANDBOX_POSTURE

        cases = _yaml.safe_load((self.BENCH / "_honesty" / "tasks.yaml").read_text()) or {}
        tasks = cases.get("tasks") or []
        assert len(tasks) >= 6, f"only {len(tasks)} honesty cases found — the scan is wrong"
        for task in tasks:
            assert task.get("execution_tenant") == SANDBOX_POSTURE, (
                f"honesty case {task['id']} does not pin the sandbox"
            )

    def test_a_suite_level_opt_out_cannot_drag_a_pinned_task(self) -> None:
        """The mechanism behind the previous test, exercised directly."""
        from robothor.engine.tools.handlers.benchmark import (
            SANDBOX_POSTURE,
            _execution_posture,
        )

        suite = {"execution_tenant": "production-read-only"}
        assert _execution_posture({"execution_tenant": "sandbox"}, suite) == SANDBOX_POSTURE


# ─── RI1: the per-task branch, in the shape every shipped opt-out has ────────


class TestAMixedSuiteRoutesEachTaskSeparately:
    """Every shipped `production-read-only` task lives in a MIXED suite.

    A uniform suite cannot test the per-task branch: with every task opted out
    ``_suite_execution_tenant`` returns None, so ``suite_tenant`` is None and
    ``exec_tenant = suite_tenant`` would be right by accident. Deleting the
    posture check on that line left the entire engine suite green — 6726
    passed, 0 red — while re-pointing every opted-out task at the sandbox AND
    re-opening its deny-list, because ``sandboxed`` drives
    ``_shape_child_config`` too.
    """

    @pytest.mark.asyncio
    async def test_each_task_gets_its_own_tenant(self) -> None:
        runner = _Recorder()
        teardowns: list[str] = []
        await _run_suite(
            runner,
            sandbox_on=True,
            tasks=2,
            task_postures=[None, "production-read-only"],
            teardowns=teardowns,
        )

        assert len(runner.calls) == 2
        sandboxed, read_only = runner.calls

        assert sandboxed.get("tenant_id") == SANDBOX
        assert sandboxed["_bound_tenant"] == SANDBOX
        assert read_only.get("tenant_id") is None, (
            "an opted-out task was re-pointed at the sandbox by its neighbour"
        )
        assert read_only["_bound_tenant"] == OWNING_TENANT

        assert teardowns == [SANDBOX], (
            f"teardown ran {len(teardowns)} times for one sandboxed task: {teardowns}"
        )

    @pytest.mark.asyncio
    async def test_the_read_only_task_keeps_the_full_deny_list(self) -> None:
        """The second-order effect: ``sandboxed`` also drives
        ``_shape_child_config``, so a routing regression re-allows CRM writes
        in the very task that is reading production."""
        runner = _Recorder()
        await _run_suite(
            runner, sandbox_on=True, tasks=2, task_postures=[None, "production-read-only"]
        )
        sandboxed, read_only = runner.calls

        read_only_denied = set(read_only["agent_config"].tools_denied)
        for tool in ("create_person", "update_person", "create_task", "resolve_task"):
            assert tool in read_only_denied, (
                f"{tool} was writable in a task reading the production tenant"
            )

        sandbox_denied = set(sandboxed["agent_config"].tools_denied)
        assert "update_person" not in sandbox_denied, (
            "the sandboxed task lost its sandbox-safe CRM writes — the two "
            "tasks are being shaped identically, which is the regression"
        )
        assert "exec" in sandbox_denied and "store_memory" in sandbox_denied

    @pytest.mark.asyncio
    async def test_both_tasks_stay_inside_the_write_boundary(self) -> None:
        runner = _Recorder()
        await _run_suite(
            runner, sandbox_on=True, tasks=2, task_postures=[None, "production-read-only"]
        )
        assert [call["_marker"] for call in runner.calls] == [True, True]

    @pytest.mark.asyncio
    async def test_the_sandbox_tenant_is_still_ensured_once(self) -> None:
        ensured: list[str] = []
        await _run_suite(
            _Recorder(),
            sandbox_on=True,
            tasks=2,
            task_postures=[None, "production-read-only"],
            ensured=ensured,
        )
        assert ensured == [SANDBOX]
