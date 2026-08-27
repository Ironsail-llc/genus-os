"""A plugin should be able to run on a schedule, not only on a tool call.

Every recurring thing the engine does is registered by the scheduler from
code inside the package: agent crons, the workflow crons, the memory
sweepers. A third-party capability could contribute a tool, a schema, a
guardrail, a hook and a model — but nothing that runs on its own. Adding
one meant editing `scheduler.py`.

DeepSeek Harness exposes background work as `ctx.jobs`, and this is one of
the extension kinds behind it.

The trap this has to avoid is already documented in this file's own
history: `reconcile_schedules` rebuilds the live job set from what the
manifests declare and removes anything else, so a job the manifests cannot
know about is culled on the first reconcile after every restart. That is
exactly what happened to `memory:write-job-sweeper`, which ran for at most
five minutes per engine lifetime until `_SYSTEM_JOB_PREFIXES` was
introduced. Plugin jobs are namespaced into that same protected set rather
than discovering the bug a second time.
"""

from __future__ import annotations

import pytest

from robothor.plugins import reload_plugins


def _job(**over):
    spec = {"cron": "0 * * * *", "func": lambda: None}
    spec.update(over)
    return spec


class _JobEP:
    group = "genus.jobs"

    def __init__(self, jobs: dict, name: str = "testjobs"):
        self.name = name
        self._jobs = jobs

    def load(self):
        return {"genus_contract_version": "1.0", "jobs": self._jobs}


@pytest.fixture
def install(monkeypatch):
    def _install(jobs: dict):
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_JobEP(jobs)])
        reload_plugins()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


class TestTheGroupExists:
    def test_the_loader_declares_it(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.jobs") == "jobs"

    def test_the_pluginset_carries_it(self, install):
        from robothor.plugins import load_plugins

        install({"nightly_sweep": _job()})
        assert "nightly_sweep" in (load_plugins(reserved_names=set()).jobs or {})


class TestReconcileNeverCullsThem:
    """The bug that ate memory:write-job-sweeper, not rediscovered."""

    def test_plugin_ids_are_in_the_protected_prefix_set(self):
        from robothor.engine.scheduler import _SYSTEM_JOB_PREFIXES

        assert "plugin:" in _SYSTEM_JOB_PREFIXES

    def test_a_plugin_job_is_not_an_agent_job(self):
        from robothor.engine.scheduler import _is_agent_job

        assert not _is_agent_job("plugin:nightly_sweep")
        assert _is_agent_job("main")


class TestRegistration:
    def test_valid_jobs_are_registered_under_a_plugin_prefix(self, install):
        from robothor.engine.scheduler import plugin_job_specs

        install({"nightly_sweep": _job(cron="30 2 * * *")})
        specs = plugin_job_specs()
        assert "plugin:nightly_sweep" in specs
        assert specs["plugin:nightly_sweep"]["cron"] == "30 2 * * *"

    def test_a_spec_missing_its_cron_is_skipped_not_raised(self, install):
        from robothor.engine.scheduler import plugin_job_specs

        install({"broken": {"func": lambda: None}, "fine": _job()})
        specs = plugin_job_specs()
        assert "plugin:fine" in specs
        assert "plugin:broken" not in specs

    def test_a_spec_missing_a_callable_is_skipped(self, install):
        from robothor.engine.scheduler import plugin_job_specs

        install({"noop": {"cron": "0 * * * *"}})
        assert plugin_job_specs() == {}

    def test_a_malformed_cron_is_skipped(self, install):
        from robothor.engine.scheduler import plugin_job_specs

        install({"bad": _job(cron="not a cron"), "good": _job()})
        specs = plugin_job_specs()
        assert "plugin:good" in specs
        assert "plugin:bad" not in specs

    def test_a_plugin_that_raises_on_load_yields_no_jobs(self, monkeypatch):
        from robothor.engine.scheduler import plugin_job_specs
        from robothor.plugins import loader

        class _Boom:
            name = "boom"
            group = "genus.jobs"

            def load(self):
                raise RuntimeError("bad package")

        monkeypatch.setattr(loader, "_discover", lambda: [_Boom()])
        reload_plugins()
        assert plugin_job_specs() == {}

    def test_removing_the_plugin_withdraws_the_job(self, install, monkeypatch):
        from robothor.engine.scheduler import plugin_job_specs

        install({"temporary": _job()})
        assert "plugin:temporary" in plugin_job_specs()

        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        assert plugin_job_specs() == {}


class TestTheSchedulerActuallyRegistersThem:
    """A spec list nothing schedules is a function, not a feature."""

    def _scheduler(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from robothor.engine.scheduler import CronScheduler

        s = CronScheduler.__new__(CronScheduler)
        s.scheduler = AsyncIOScheduler()
        return s

    def test_plugin_jobs_reach_apscheduler(self, install):
        calls = []
        install({"sweep": _job(cron="*/5 * * * *", func=lambda: calls.append(1))})

        s = self._scheduler()
        registered = s.register_plugin_jobs()

        assert registered == 1
        ids = {j.id for j in s.scheduler.get_jobs()}
        assert "plugin:sweep" in ids, f"not scheduled: {ids}"

    def test_registration_is_idempotent_across_a_reload(self, install):
        install({"sweep": _job()})
        s = self._scheduler()
        s.register_plugin_jobs()
        s.register_plugin_jobs()
        ids = [j.id for j in s.scheduler.get_jobs()]
        assert ids.count("plugin:sweep") == 1, "a reload duplicated the job"

    def test_a_withdrawn_job_is_removed_on_re_registration(self, install, monkeypatch):
        install({"temporary": _job()})
        s = self._scheduler()
        s.register_plugin_jobs()
        assert "plugin:temporary" in {j.id for j in s.scheduler.get_jobs()}

        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        s.register_plugin_jobs()
        assert "plugin:temporary" not in {j.id for j in s.scheduler.get_jobs()}

    def test_a_broken_plugin_does_not_stop_the_scheduler(self, monkeypatch):
        from robothor.plugins import loader

        class _Boom:
            name = "boom"
            group = "genus.jobs"

            def load(self):
                raise RuntimeError("bad package")

        monkeypatch.setattr(loader, "_discover", lambda: [_Boom()])
        reload_plugins()
        s = self._scheduler()
        assert s.register_plugin_jobs() == 0  # must not raise


class TestItIsReachable:
    """Registration that nothing calls is the defect this repo keeps finding."""

    def test_start_registers_plugin_jobs(self):
        """`start()` must call it, or plugin jobs only exist in a test."""
        import inspect

        from robothor.engine.scheduler import CronScheduler

        assert "register_plugin_jobs" in inspect.getsource(CronScheduler.start), (
            "CronScheduler.start never registers plugin jobs"
        )

    def test_the_reload_signal_re_registers_them(self):
        """SIGHUP must re-register, or a hot-installed job never schedules."""
        import inspect

        from robothor.engine import daemon

        assert "register_plugin_jobs" in inspect.getsource(daemon), (
            "the reload path never re-registers plugin jobs"
        )
