"""Reconcile must ADD and REPLACE jobs, not only prune them.

Before this, ``reconcile()`` was prune-only: it built the set of ids a manifest
still justifies, deleted the stale DB rows, removed the orphan APScheduler
jobs, and returned. Nothing in the engine ever added a job after boot. So the
shipped setup wizard's ``POST /api/setup/agent`` and the Helm's
``installed_agents`` install both wrote a manifest that would not fire until
someone restarted the engine — a write that reports success and changes
nothing, which is the failure shape this codebase keeps finding.

The 2026-08-23 interlock is the constraint the whole file is written around: a
dirty scan must still add nothing, replace nothing and prune nothing. "Add" is
safe in a way "prune" is not, but a scan that cannot see every manifest also
cannot be trusted about what a job's trigger should be, and half-reconciling
from a partial view is how an agent ends up on a stale schedule nobody can
explain.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MANIFEST = """id: {id}
name: {name}
description: A demo agent
version: "2026-09-12"
department: operations
model:
  primary: openrouter/example/demo-model
schedule:
  cron: "{cron}"
  timezone: {timezone}
delivery:
  mode: none
tools_allowed: [read_file]
instruction_file: brain/DEMO.md
"""

DISABLED = "  enabled: false\n"


def write_manifest(
    manifest_dir,
    agent_id="demo-agent",
    *,
    name="Demo Agent",
    cron="0 9 * * *",
    timezone="UTC",
    schedule_extra="",
    extra="",
):
    """One agent manifest in ``manifest_dir``, returning its path."""
    body = MANIFEST.format(id=agent_id, name=name, cron=cron, timezone=timezone)
    if schedule_extra:
        body = body.replace("delivery:\n", schedule_extra + "delivery:\n")
    path = manifest_dir / f"{agent_id}.yaml"
    path.write_text(body + extra)
    return path


@pytest.fixture
def fleet(tmp_path):
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    return manifest_dir


@pytest.fixture
def scheduler(fleet):
    """A real ``AsyncIOScheduler``-backed CronScheduler over an empty fleet."""
    from robothor.engine.config import EngineConfig
    from robothor.engine.scheduler import CronScheduler

    config = EngineConfig(manifest_dir=fleet, workspace=fleet.parent.parent, tenant_id="default")
    return CronScheduler(config, MagicMock())


@pytest.fixture
def _no_db():
    """The DB half of reconcile, stubbed. The job half is what is under test."""
    with (
        patch("robothor.engine.scheduler.upsert_schedule", return_value=True) as upsert,
        patch("robothor.engine.scheduler.delete_stale_schedules", return_value=[]),
    ):
        yield upsert


def trigger_of(scheduler, job_id):
    job = scheduler.scheduler.get_job(job_id)
    assert job is not None, f"no job {job_id}"
    return job.trigger


@pytest.mark.usefixtures("_no_db")
class TestAddAndReplace:
    def test_reconcile_adds_a_new_cron_job_without_restart(self, scheduler, fleet):
        write_manifest(fleet)

        result = scheduler.reconcile_schedules()

        assert result.added == ["demo-agent"]
        assert result.replaced == []
        assert scheduler.scheduler.get_job("demo-agent") is not None

    def test_reconcile_replaces_the_job_when_the_cron_changes(self, scheduler, fleet):
        write_manifest(fleet)
        scheduler.reconcile_schedules()
        before = repr(trigger_of(scheduler, "demo-agent"))

        write_manifest(fleet, cron="30 17 * * *")
        result = scheduler.reconcile_schedules()

        assert result.replaced == ["demo-agent"]
        assert result.added == []
        assert repr(trigger_of(scheduler, "demo-agent")) != before

    def test_reconcile_replaces_the_job_when_the_timezone_changes_only(self, scheduler, fleet):
        """``str(CronTrigger)`` omits the timezone, so a tz-only edit is the
        case a naive comparison silently misses — and a nine-o'clock agent
        quietly keeps firing on the old zone."""
        write_manifest(fleet, timezone="UTC")
        scheduler.reconcile_schedules()
        before = str(trigger_of(scheduler, "demo-agent").timezone)

        write_manifest(fleet, timezone="America/New_York")
        result = scheduler.reconcile_schedules()

        assert result.replaced == ["demo-agent"]
        assert str(trigger_of(scheduler, "demo-agent").timezone) != before

    def test_reconcile_replaces_heartbeat_and_worker_jobs_independently(self, scheduler, fleet):
        extra = (
            'heartbeat:\n  cron: "0 * * * *"\n  instruction_file: brain/HB.md\n'
            'worker:\n  cron: "0 7 * * *"\n  instruction_file: brain/WK.md\n'
        )
        write_manifest(fleet, extra=extra)
        first = scheduler.reconcile_schedules()
        assert sorted(first.added) == ["demo-agent", "demo-agent:heartbeat", "demo-agent:worker"]

        changed = (
            'heartbeat:\n  cron: "30 * * * *"\n  instruction_file: brain/HB.md\n'
            'worker:\n  cron: "0 7 * * *"\n  instruction_file: brain/WK.md\n'
        )
        write_manifest(fleet, extra=changed)
        second = scheduler.reconcile_schedules()

        assert second.replaced == ["demo-agent:heartbeat"]
        assert second.added == []

    def test_reconcile_is_idempotent(self, scheduler, fleet):
        write_manifest(fleet)
        scheduler.reconcile_schedules()

        again = scheduler.reconcile_schedules()

        assert again.added == []
        assert again.replaced == []
        assert again.pruned == []

    def test_reconcile_prunes_a_retired_agent(self, scheduler, fleet):
        write_manifest(fleet)
        scheduler.reconcile_schedules()

        (fleet / "demo-agent.yaml").unlink()
        result = scheduler.reconcile_schedules()

        assert result.pruned == ["demo-agent"]
        assert scheduler.scheduler.get_job("demo-agent") is None

    def test_system_prefixed_jobs_are_never_touched(self, scheduler, fleet):
        write_manifest(fleet)
        scheduler.scheduler.add_job(
            lambda: None, trigger="interval", minutes=5, id="memory:write-job-sweeper"
        )

        result = scheduler.reconcile_schedules()

        assert result.pruned == []
        assert "memory:write-job-sweeper" not in result.added + result.replaced
        assert scheduler.scheduler.get_job("memory:write-job-sweeper") is not None

    def test_upsert_records_the_schedule_row_for_an_added_job(self, scheduler, fleet, _no_db):
        write_manifest(fleet)

        scheduler.reconcile_schedules()

        assert _no_db.call_count == 1
        assert _no_db.call_args.kwargs["agent_id"] == "demo-agent"
        assert _no_db.call_args.kwargs["cron_expr"] == "0 9 * * *"
        assert _no_db.call_args.kwargs["enabled"] is True

    def test_one_job_that_cannot_be_registered_costs_that_job_and_no_other(
        self, scheduler, fleet, _no_db
    ):
        """A manifest is operator input. An unguarded raise out of ``add_job``
        would take the whole reconcile with it — the other agents' additions
        AND the prune — which is how one bad file becomes a fleet outage."""
        write_manifest(fleet, "demo-agent")
        write_manifest(fleet, "second-agent", name="Second Agent", cron="0 10 * * *")
        real_add = scheduler.scheduler.add_job

        def _explode(*args, **kwargs):
            if kwargs.get("id") == "demo-agent":
                raise RuntimeError("apscheduler said no")
            return real_add(*args, **kwargs)

        with patch.object(scheduler.scheduler, "add_job", _explode):
            result = scheduler.reconcile_schedules()

        assert result.added == ["second-agent"]
        assert scheduler.scheduler.get_job("second-agent") is not None
        assert scheduler.scheduler.get_job("demo-agent") is None
        # The row is only written for the job that actually exists.
        assert [call.kwargs["agent_id"] for call in _no_db.call_args_list] == ["second-agent"]


@pytest.mark.usefixtures("_no_db")
class TestAnEditThatDoesNotMoveTheTrigger:
    """``agent_schedules`` carries more than the trigger, so "the job is
    unchanged" is not "the row is unchanged".

    ``job_matches`` compares ``repr(trigger)`` and the misfire grace, which is
    the right question for APScheduler and the wrong one for the database:
    ``JobSpec.upsert`` also carries ``model_primary``, ``model_fallbacks``, the
    three delivery fields, ``session_target`` and ``timeout_seconds``. Editing
    any of them reconciled to a no-op, the row kept the old value, and the Helm
    answered ``applied: true`` — the exact "saved and in effect are two
    different fields" failure this surface exists to remove. Those columns are
    what `routers/fleet.py`, `routers/agents.py` and `scripts/gen_cron_map.py`
    read, so the appliance's own state table disagreed with the manifest until
    the next restart.
    """

    def _model(self, model: str) -> str:
        return f"model:\n  primary: {model}\n"

    def test_a_model_only_edit_rewrites_the_schedule_row(self, scheduler, fleet, _no_db):
        write_manifest(fleet)
        scheduler.reconcile_schedules()
        _no_db.reset_mock()

        path = fleet / "demo-agent.yaml"
        path.write_text(
            path.read_text().replace(
                "primary: openrouter/example/demo-model", "primary: openrouter/example/other"
            )
        )
        result = scheduler.reconcile_schedules()

        assert _no_db.call_count == 1, "the row was never rewritten"
        assert _no_db.call_args.kwargs["model_primary"] == "openrouter/example/other"
        assert result.refreshed == ["demo-agent"], result
        assert result.added == [] and result.replaced == []

    def test_a_delivery_only_edit_rewrites_the_schedule_row(self, scheduler, fleet, _no_db):
        write_manifest(fleet)
        scheduler.reconcile_schedules()
        _no_db.reset_mock()

        path = fleet / "demo-agent.yaml"
        path.write_text(
            path.read_text().replace(
                "delivery:\n  mode: none",
                'delivery:\n  mode: announce\n  channel: telegram\n  to: "1"',
            )
        )
        scheduler.reconcile_schedules()

        assert _no_db.call_count == 1
        assert _no_db.call_args.kwargs["delivery_mode"] == "announce"

    def test_a_genuinely_unchanged_manifest_still_reconciles_to_nothing(
        self, scheduler, fleet, _no_db
    ):
        """The counter-case. Rewriting the row on every pass would make this
        class vacuous and turn a five-minute watchdog into a write loop."""
        write_manifest(fleet)
        scheduler.reconcile_schedules()
        _no_db.reset_mock()

        result = scheduler.reconcile_schedules()

        assert _no_db.call_count == 0
        assert result.added == [] and result.replaced == []
        assert result.refreshed == []
        assert result.touched() == 0

    def test_a_trigger_edit_is_replaced_not_merely_refreshed(self, scheduler, fleet, _no_db):
        """The two lists stay distinct: one means the job moved, the other
        means only the row did."""
        write_manifest(fleet)
        scheduler.reconcile_schedules()

        write_manifest(fleet, cron="30 17 * * *")
        result = scheduler.reconcile_schedules()

        assert result.replaced == ["demo-agent"]
        assert result.refreshed == []


@pytest.mark.usefixtures("_no_db")
class TestTheDirtyScanInterlock:
    """A scan that cannot see every manifest is authority for nothing."""

    def _break_one(self, fleet):
        (fleet / "broken.yaml").write_text("id: broken\nname: [unclosed\n")

    def test_dirty_scan_adds_nothing_and_prunes_nothing_and_reports_blocked(self, scheduler, fleet):
        write_manifest(fleet)
        scheduler.reconcile_schedules()
        self._break_one(fleet)
        write_manifest(fleet, "second-agent", name="Second Agent")

        result = scheduler.reconcile_schedules()

        assert result.added == []
        assert result.replaced == []
        assert result.pruned == []
        assert result.clean is False
        assert "broken" in result.blocked
        # The job that was already live survives — refusing to act is not the
        # same as tearing down.
        assert scheduler.scheduler.get_job("demo-agent") is not None
        assert scheduler.scheduler.get_job("second-agent") is None

    def test_an_unreadable_directory_blocks_everything_under_one_key(self, scheduler, fleet):
        import shutil

        shutil.rmtree(fleet)

        result = scheduler.reconcile_schedules()

        assert result.blocked == {"*": "manifest directory unreadable"}
        assert result.added == result.replaced == result.pruned == []

    def test_blocked_reason_carries_no_path_or_manifest_value(self, scheduler, fleet):
        """Rules 1 and 2: a reason reaches an operator page and a browser."""
        write_manifest(fleet)
        self._break_one(fleet)

        result = scheduler.reconcile_schedules()

        rendered = repr(result.blocked)
        assert str(fleet) not in rendered
        assert ".yaml" not in rendered
        assert "unclosed" not in rendered
        assert result.blocked["broken"] == "YAMLError"


@pytest.mark.usefixtures("_no_db")
class TestScheduleEnabled:
    """``schedule.enabled: false`` has to mean something.

    The bridge's fleet view and the doctor have both displayed
    ``agent_schedules.enabled`` since long before anything wrote a value other
    than a literal ``True`` — a switch on a dashboard that was wired to
    nothing.
    """

    def test_disabled_schedule_registers_no_job(self, scheduler, fleet):
        write_manifest(fleet, schedule_extra=DISABLED)

        result = scheduler.reconcile_schedules()

        assert result.added == []
        assert scheduler.scheduler.get_job("demo-agent") is None

    def test_reconcile_removes_the_job_when_schedule_is_disabled(self, scheduler, fleet):
        write_manifest(fleet)
        scheduler.reconcile_schedules()
        assert scheduler.scheduler.get_job("demo-agent") is not None

        write_manifest(fleet, schedule_extra=DISABLED)
        result = scheduler.reconcile_schedules()

        assert result.pruned == ["demo-agent"]
        assert scheduler.scheduler.get_job("demo-agent") is None

    def test_disabling_also_stops_the_heartbeat_and_the_worker(self, scheduler, fleet):
        """A half-silenced agent is worse than either state: the operator
        turned it off and it kept talking."""
        extra = (
            'heartbeat:\n  cron: "0 * * * *"\n  instruction_file: brain/HB.md\n'
            'worker:\n  cron: "0 7 * * *"\n  instruction_file: brain/WK.md\n'
        )
        write_manifest(fleet, extra=extra)
        scheduler.reconcile_schedules()

        write_manifest(fleet, schedule_extra=DISABLED, extra=extra)
        result = scheduler.reconcile_schedules()

        assert sorted(result.pruned) == [
            "demo-agent",
            "demo-agent:heartbeat",
            "demo-agent:worker",
        ]
        assert scheduler.scheduler.get_jobs() == []

    @pytest.mark.asyncio
    async def test_boot_writes_a_disabled_row_and_schedules_nothing(self, scheduler, fleet, _no_db):
        """``start()`` has to agree with reconcile, or a disabled agent comes
        back on every restart — and the row has to exist either way, because a
        silenced agent that vanished from ``agent_schedules`` is one the fleet
        view cannot tell apart from a deleted one."""
        write_manifest(fleet, schedule_extra=DISABLED)

        with (
            patch.object(scheduler.scheduler, "start"),
            patch("robothor.engine.scheduler.alert_manifest_scan", new_callable=AsyncMock),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await scheduler.start()

        # The engine's own infrastructure jobs are registered regardless; the
        # agent's are what must be absent.
        agent_jobs = [job.id for job in scheduler.scheduler.get_jobs() if "demo-agent" in job.id]
        assert agent_jobs == []
        assert _no_db.call_args.kwargs["agent_id"] == "demo-agent"
        assert _no_db.call_args.kwargs["enabled"] is False

    def test_the_manifest_answer_reaches_the_agent_config(self, fleet):
        """The column the fleet view reads must carry the manifest's answer."""
        from robothor.engine.config import load_manifest_dir, manifest_to_agent_config

        write_manifest(fleet, schedule_extra=DISABLED)
        scan = load_manifest_dir(fleet)
        config = manifest_to_agent_config(scan.manifests[0])

        assert config.schedule_enabled is False


class TestAddingFromAWorkerThread:
    """R1's real risk: reconcile's body runs in an executor thread.

    ``AsyncIOScheduler.wakeup`` is decorated ``@run_in_event_loop``, which
    hands the call to ``loop.call_soon_threadsafe``, and ``_real_add_job``
    takes ``_jobstores_lock`` — so a cross-thread ``add_job`` is safe by
    construction in apscheduler 3.x. "By construction" is exactly the kind of
    claim this repo has been wrong about before, so it gets a test that
    actually crosses the boundary against a RUNNING scheduler.
    """

    @pytest.mark.asyncio
    async def test_add_job_from_a_worker_thread_reaches_the_running_scheduler(
        self, scheduler, fleet, _no_db
    ):
        write_manifest(fleet)
        scheduler.scheduler.start()
        try:
            with patch("robothor.engine.scheduler.alert_manifest_scan"):
                result = await scheduler.reconcile()
            assert result.added == ["demo-agent"]
            job = scheduler.scheduler.get_job("demo-agent")
            assert job is not None
            assert job.next_run_time is not None
        finally:
            scheduler.scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_reconcile_runs_its_blocking_work_off_the_event_loop(
        self, scheduler, fleet, _no_db
    ):
        """Rule 10. The loader and the DB prune are both blocking."""
        write_manifest(fleet)
        main_thread = threading.get_ident()
        seen: list[int] = []
        real = type(scheduler)._reconcile_from_scan

        def _spy(self, scan):
            seen.append(threading.get_ident())
            return real(self, scan)

        with (
            patch("robothor.engine.scheduler.alert_manifest_scan"),
            patch.object(type(scheduler), "_reconcile_from_scan", _spy),
        ):
            await scheduler.reconcile()

        assert seen and main_thread not in seen
