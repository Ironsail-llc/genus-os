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
row is the grade ledger and belongs to the tenant that owns the fleet, not to
the sandbox the agent was graded in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
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


def _suite(*, fixtures: bool = False) -> str:
    task: dict[str, Any] = {
        "id": "t1",
        "prompt": "x",
        "category": "correctness",
        "weight": 1.0,
        "expected": {"must_contain": ["ok"]},
    }
    suite: dict[str, Any] = {
        "id": "s1",
        "agent_id": "email-analyst",
        "max_cost_usd": 1.0,
        "tasks": [task],
    }
    if fixtures:
        task["fixtures"] = ["a_person"]
        suite["fixtures"] = {
            "fixtures": {
                "a_person": {"table": "crm_people", "values": {"email": "alice@example.com"}}
            }
        }
    return json.dumps(suite)


def _child_config() -> MagicMock:
    cfg = MagicMock()
    cfg.max_iterations = 10
    cfg.tools_allowed = ["read_file"]
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
) -> None:
    """Drive ``_benchmark_run`` end to end with the database stubbed out."""
    from robothor.engine.benchmark_sandbox import SeededFixtures
    from robothor.engine.tools.handlers import benchmark as bench

    store, read_fn, write_fn = _mock_blocks()
    store["benchmark:email-analyst:s1"] = _suite(fixtures=fixtures)

    def _ensure(tenant_id: str | None = None) -> str:
        if ensured is not None:
            ensured.append(tenant_id or seeded_tenant)
        return tenant_id or seeded_tenant

    def _seed(spec: Any, keys: Any, tenant_id: str | None = None) -> SeededFixtures:
        return SeededFixtures(tenant_id=tenant_id or seeded_tenant)

    def _teardown(tenant_id: str | None = None) -> int:
        if teardowns is not None:
            teardowns.append(tenant_id or seeded_tenant)
        return 0

    import robothor.engine.benchmark_sandbox as bs

    with (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=runner),
        patch("robothor.engine.config.load_agent_config", return_value=_child_config()),
        patch.object(bench, "sandbox_active", return_value=sandbox_on),
        patch.object(bs, "ensure_sandbox_tenant", side_effect=_ensure),
        patch.object(bs, "seed_fixtures", side_effect=_seed),
        patch.object(bs, "teardown_sandbox", side_effect=_teardown),
        patch.object(bench, "_write_benchmark_result_row"),
    ):
        result = await bench._benchmark_run(
            {"agent_id": "email-analyst", "suite_id": "s1", "tag": "t"}, CTX
        )
    assert not result.get("error"), result


@pytest.fixture(autouse=True)
def _owning_tenant(monkeypatch: Any) -> None:
    """The instance's own tenant, so a leak into it is visible by name."""
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", OWNING_TENANT)
    monkeypatch.setenv("ROBOTHOR_BENCHMARK_TENANT", SANDBOX)


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
        metric AND put it in a tenant teardown sweeps."""
        from robothor.db.connection import effective_tenant
        from robothor.engine.tools.handlers import benchmark as bench

        seen: list[str] = []

        def _record(**_kwargs: Any) -> None:
            seen.append(effective_tenant())

        runner = _Recorder()
        store, read_fn, write_fn = _mock_blocks()
        store["benchmark:email-analyst:s1"] = _suite()
        import robothor.engine.benchmark_sandbox as bs

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_fn),
            patch("robothor.memory.blocks.write_block", side_effect=write_fn),
            patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=runner),
            patch("robothor.engine.config.load_agent_config", return_value=_child_config()),
            patch.object(bench, "sandbox_active", return_value=True),
            patch.object(bs, "ensure_sandbox_tenant", return_value=SANDBOX),
            patch.object(bs, "teardown_sandbox", return_value=0),
            patch.object(bench, "_write_benchmark_result_row", side_effect=_record),
        ):
            await bench._benchmark_run(
                {"agent_id": "email-analyst", "suite_id": "s1", "tag": "t"}, CTX
            )

        assert seen == [OWNING_TENANT], (
            "the benchmark_results row was written inside the sandbox tenant "
            "scope — the grade ledger belongs to the tenant that owns the fleet"
        )

    def test_the_results_insert_still_takes_the_tenant_default(self) -> None:
        """The INSERT deliberately omits ``tenant_id`` (migration 098 keeps the
        column DEFAULT for exactly this writer). If a tenant is ever named
        here it must be the owning one, never the sandbox."""
        import robothor.engine.tools.handlers.benchmark as bench

        src = Path(bench.__file__).read_text()
        start = src.index("INSERT INTO benchmark_results")
        insert = src[start : src.index('"""', start)]
        assert "tenant_id" not in insert
        assert "sandbox" not in insert.lower()
