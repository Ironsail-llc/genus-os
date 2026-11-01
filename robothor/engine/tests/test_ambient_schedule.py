"""Ambient scheduling: one job per opted-in agent, and none while the flag is off."""

from __future__ import annotations

import pytest

from robothor.engine.ambient import AmbientConfig, ambient_trigger
from robothor.engine.models import AgentConfig
from robothor.engine.schedule_reconcile import KIND_AMBIENT, agent_job_specs, desired_job_specs


def _ambient(digest_at: str = "0 9,15 * * *") -> AmbientConfig:
    return AmbientConfig(
        watch=["inbox"],
        stalled_after="4h",
        stalled_after_seconds=14400,
        digest_at=digest_at,
        tiers={"nudge_thread": "approve", "send_digest": "approve", "reply_on_behalf": "never"},
    )


def _fleet() -> list[AgentConfig]:
    return [
        AgentConfig(id="alpha", name="Alpha", ambient=_ambient()),
        AgentConfig(id="beta", name="Beta", ambient=_ambient("08:00, 17:30")),
        AgentConfig(id="gamma", name="Gamma", cron_expr="0 * * * *"),
    ]


def test_flag_on_gives_one_ambient_job_per_opted_in_agent(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_AMBIENT", "observe")
    specs, errors = desired_job_specs(_fleet())
    assert errors == []
    ambient = {s.job_id: s for s in specs.values() if s.kind == KIND_AMBIENT}
    assert sorted(ambient) == ["alpha:ambient", "beta:ambient"]
    assert ambient["alpha:ambient"].agent_id == "alpha"
    # gamma's own cron job is untouched
    assert "gamma" in specs


@pytest.mark.parametrize("value", [None, "", "off", "nonsense"])
def test_flag_off_gives_no_ambient_jobs(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ROBOTHOR_AMBIENT", raising=False)
    else:
        monkeypatch.setenv("ROBOTHOR_AMBIENT", value)
    specs, _ = desired_job_specs(_fleet())
    assert [s for s in specs.values() if s.kind == KIND_AMBIENT] == []


def test_a_bad_digest_time_costs_that_job_only(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_AMBIENT", "observe")
    agent = AgentConfig(id="alpha", name="Alpha", cron_expr="0 * * * *", ambient=_ambient("nope"))
    specs, errors = agent_job_specs(agent)
    assert list(specs) == ["alpha"]
    assert [(kind, agent_id) for kind, agent_id, _, _ in errors] == [(KIND_AMBIENT, "alpha")]


def test_times_fire_at_each_listed_time_and_nowhere_else():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    trigger = ambient_trigger("08:00, 17:30", timezone="UTC")
    start = datetime(2026, 10, 31, 0, 0, tzinfo=tz)
    fires = []
    previous = None
    now = start
    for _ in range(3):
        nxt = trigger.get_next_fire_time(previous, now)
        fires.append(nxt.strftime("%H:%M"))
        previous = nxt
        now = nxt
    assert fires == ["08:00", "17:30", "08:00"]


def test_the_scheduler_has_a_handler_for_the_ambient_kind():
    from unittest.mock import MagicMock

    from robothor.engine.scheduler import CronScheduler

    sched = CronScheduler(MagicMock(default_timezone="UTC"), MagicMock())
    assert callable(sched._job_handler(KIND_AMBIENT))
