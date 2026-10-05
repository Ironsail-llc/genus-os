"""The engine resumes interrupted coding jobs at startup, and stops them cleanly."""

from __future__ import annotations

from robothor.engine import daemon
from robothor.engine.coding import jobs as jobs_mod


async def test_startup_resume_calls_the_manager(monkeypatch):
    seen = {}

    async def fake_resume(tenant_id=None):
        seen["tenant"] = tenant_id
        return 3

    monkeypatch.setattr(jobs_mod, "resume_interrupted_jobs", fake_resume)
    assert await daemon._resume_coding_jobs() == 3
    assert seen == {"tenant": None}


async def test_startup_resume_failure_is_never_fatal(monkeypatch):
    async def broken(tenant_id=None):
        raise RuntimeError("relation coding_jobs does not exist")

    monkeypatch.setattr(jobs_mod, "resume_interrupted_jobs", broken)
    assert await daemon._resume_coding_jobs() == 0


async def test_shutdown_stops_jobs_without_marking_them(monkeypatch):
    calls = []

    class FakeManager:
        async def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(jobs_mod, "_manager", FakeManager())
    await daemon._stop_coding_jobs()
    assert calls == ["shutdown"]


async def test_shutdown_without_a_manager_does_nothing(monkeypatch):
    monkeypatch.setattr(jobs_mod, "_manager", None)
    await daemon._stop_coding_jobs()
    assert jobs_mod._manager is None  # never built just to be stopped
