"""A self-improve finding must never be filed to an agent that cannot run.

`open_task_for_finding` hardcoded `assigned_to_agent="auto-agent"`. auto-agent's
manifest carries `schedule.enabled: False` and an empty cron, so it has not run
in production once in 30 days -- its only runs are benchmark sub-runs. The
findings were filed anyway: 35 open tasks accumulated between 2026-08-17 and
2026-08-21, none of them reachable by any agent.

That is the failure this campaign keeps finding in new clothes -- a hardcoded
name that drifted from what actually exists, with nothing checking. The task
looked delegated, so nobody looked again.

The executor is now resolved against the manifests at call time, and a finding
is refused loudly rather than filed into a void.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from robothor.engine import buddy_critic

if TYPE_CHECKING:
    from pathlib import Path


def _finding() -> buddy_critic.Finding:
    return buddy_critic.Finding(
        agent_id="email-analyst",
        dimension="correctness",
        metric="tool_success_rate",
        severity=7.0,
        consecutive_days_breached=3,
        baseline_metric=0.42,
        target=">=0.90",
        representative_run_ids=["run-1"],
        representative_feedback=["tool errors on every thread fetch"],
        corrective_actions=["review the gog error path"],
    )


def _write_manifest(agents_dir: Path, agent_id: str, schedule: dict) -> None:
    (agents_dir / f"{agent_id}.yaml").write_text(
        yaml.safe_dump({"id": agent_id, "schedule": schedule})
    )


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(buddy_critic, "AGENTS_DIR", d)
    return d


def test_disabled_schedule_cannot_run(agents_dir: Path) -> None:
    """The exact shape of auto-agent's manifest: enabled False, empty cron."""
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    assert buddy_critic._agent_can_run("auto-agent") is False


def test_empty_cron_cannot_run(agents_dir: Path) -> None:
    """A manifest with no cron is not scheduled, whatever `enabled` says."""
    _write_manifest(agents_dir, "ghost", {"cron": "   "})
    assert buddy_critic._agent_can_run("ghost") is False


def test_missing_manifest_is_unknown_not_disabled(agents_dir: Path) -> None:
    """docs/agents/ is gitignored instance-land -- it may not exist at all.

    Treating "no manifest" as "cannot run" refuses every finding on a clean
    checkout, which is how the first version of this fix broke CI.
    """
    assert buddy_critic._agent_can_run("does-not-exist") is None


def test_scheduled_agent_can_run(agents_dir: Path) -> None:
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})
    assert buddy_critic._agent_can_run("agent-architect") is True


def test_executor_skips_the_dead_one_and_picks_the_live_one(agents_dir: Path) -> None:
    """Preference order is honoured, but only among agents that can run."""
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})
    assert buddy_critic.resolve_self_improve_executor() == "agent-architect"


def test_preferred_executor_wins_when_it_can_run(agents_dir: Path) -> None:
    _write_manifest(agents_dir, "auto-agent", {"cron": "0 5 * * *"})
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})
    assert buddy_critic.resolve_self_improve_executor() == "auto-agent"


def test_no_live_executor_returns_none(agents_dir: Path) -> None:
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    _write_manifest(agents_dir, "agent-architect", {"enabled": False, "cron": ""})
    assert buddy_critic.resolve_self_improve_executor() is None


def test_finding_is_not_filed_when_no_executor_can_run(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No task at all beats a task nothing can pick up."""
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    _write_manifest(agents_dir, "agent-architect", {"enabled": False, "cron": ""})

    created: list[dict] = []
    monkeypatch.setattr("robothor.crm.dal.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr(
        "robothor.crm.dal.create_task",
        lambda **kw: created.append(kw) or "task-id",
        raising=False,
    )

    finding = _finding()
    assert buddy_critic.open_task_for_finding(finding) is None
    assert created == [], "a finding was filed to an agent that cannot run"


def test_finding_is_assigned_to_the_live_executor(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})

    created: list[dict] = []
    monkeypatch.setattr("robothor.crm.dal.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr(
        "robothor.crm.dal.create_task",
        lambda **kw: created.append(kw) or "task-id",
        raising=False,
    )

    finding = _finding()
    buddy_critic.open_task_for_finding(finding)
    assert len(created) == 1
    assert created[0]["assigned_to_agent"] == "agent-architect"


def test_executor_is_never_assigned_a_finding_about_itself(agents_dir: Path) -> None:
    """The pipeline must not edit its own harness.

    `test_skips_meta_agents` already keeps the loop from filing findings against
    its own supervisors. Resolving the executor dynamically opens the mirror
    case: agent-architect is a legal *target*, so once it becomes the executor
    it could be assigned to optimise itself.
    """
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})
    assert buddy_critic.resolve_self_improve_executor(exclude="agent-architect") is None


def test_exclusion_falls_through_to_another_live_executor(agents_dir: Path) -> None:
    """With two live executors, a finding about one goes to the other."""
    _write_manifest(agents_dir, "auto-agent", {"cron": "0 5 * * *"})
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})
    assert buddy_critic.resolve_self_improve_executor(exclude="auto-agent") == "agent-architect"


def test_finding_about_the_only_executor_is_refused(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(agents_dir, "auto-agent", {"enabled": False, "cron": ""})
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})

    created: list[dict] = []
    monkeypatch.setattr("robothor.crm.dal.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr(
        "robothor.crm.dal.create_task",
        lambda **kw: created.append(kw) or "task-id",
        raising=False,
    )

    finding = _finding()
    finding.agent_id = "agent-architect"
    assert buddy_critic.open_task_for_finding(finding) is None
    assert created == [], "the executor was assigned to fix itself"


def test_no_manifests_at_all_still_files_the_finding(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean checkout has no docs/agents/ -- the loop must still work.

    This is the case that broke CI on the first version: every candidate read
    as "cannot run", so `open_task_for_finding` refused every finding.
    """
    created: list[dict] = []
    monkeypatch.setattr("robothor.crm.dal.list_tasks", lambda **kw: [], raising=False)
    monkeypatch.setattr(
        "robothor.crm.dal.create_task",
        lambda **kw: created.append(kw) or "task-id",
        raising=False,
    )
    assert buddy_critic.resolve_self_improve_executor() == "auto-agent"
    assert buddy_critic.open_task_for_finding(_finding()) == "task-id"
    assert len(created) == 1


def test_a_known_runnable_agent_beats_an_unknown_one(agents_dir: Path) -> None:
    """auto-agent has no manifest here; agent-architect is known-scheduled."""
    _write_manifest(agents_dir, "agent-architect", {"cron": "0 3 * * *"})
    assert buddy_critic.resolve_self_improve_executor() == "agent-architect"
