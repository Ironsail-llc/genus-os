"""Where one run's `exec` goes when the sandbox playground is on.

The legacy decision (`resolve_sandbox_decision`) still says WHEN a container is
wanted; the playground decides what happens when it is wanted and there is no
runtime to start one. An owner or admin on a root run is not killed for the
engine's missing podman: the command runs on the engine host, under their
permissions, and says so. Anyone else is refused per call, told which gate
stopped them and how to unlock it. The run itself is never failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from robothor.engine import sandbox_policy as sp
from robothor.engine.sandbox import RuntimeStatus

PRESENT = RuntimeStatus(binary="podman", available=True, reason="")
ABSENT = RuntimeStatus(
    binary="podman", available=False, reason="neither podman nor docker is installed"
)


@dataclass
class _Cfg:
    id: str = "example-agent"
    sandbox: str = "docker"
    tools_allowed: list[str] = field(default_factory=lambda: ["exec"])
    tools_denied: list[str] = field(default_factory=list)


def _plan(cfg=None, *, role="owner", runtime=ABSENT, mode="off", root=True, bench=False):
    return sp.resolve_execution_plan(
        cfg or _Cfg(),
        mode,
        agent_id="example-agent",
        user_role=role,
        runtime=runtime,
        is_root_run=root,
        is_benchmark=bench,
    )


def test_the_flag_is_off_by_default(monkeypatch) -> None:
    from robothor.engine.feature_flags import sandbox_playground_enabled

    monkeypatch.delenv("ROBOTHOR_SANDBOX_PLAYGROUND", raising=False)
    assert sandbox_playground_enabled() is False
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")
    assert sandbox_playground_enabled() is True


@pytest.mark.parametrize("role", ["owner", "admin", "member", ""])
def test_a_present_runtime_contains_everyone_who_asked_for_it(role) -> None:
    plan = _plan(role=role, runtime=PRESENT)
    assert plan.where == "sandbox"
    assert plan.degraded is False


@pytest.mark.parametrize("role", ["owner", "admin", "Owner"])
def test_no_runtime_runs_an_owner_or_admin_on_the_host_and_says_so(role) -> None:
    plan = _plan(role=role)
    assert plan.where == "host"
    assert plan.degraded is True
    assert "no container runtime" in plan.reason
    assert "neither podman nor docker" in plan.reason


def test_no_runtime_refuses_a_member_naming_the_gate_and_the_unlock() -> None:
    plan = _plan(role="member")
    assert plan.where == "refuse"
    assert "no sandbox runtime" in plan.reason
    assert "member" in plan.reason
    assert "owner or admin" in plan.unlock
    assert "install" in plan.unlock


def test_no_runtime_refuses_an_automated_run_that_asked_for_a_container() -> None:
    plan = _plan(role="")
    assert plan.where == "refuse"
    assert "automated" in plan.reason


def test_a_sub_agent_never_falls_back_to_the_host() -> None:
    plan = _plan(role="owner", root=False)
    assert plan.where == "refuse"
    assert "sub-agent" in plan.reason


def test_a_benchmark_run_never_falls_back_to_the_host() -> None:
    plan = _plan(role="owner", bench=True)
    assert plan.where == "refuse"


@pytest.mark.parametrize("runtime", [PRESENT, ABSENT])
@pytest.mark.parametrize("role", ["owner", "member", ""])
def test_a_manifest_that_declares_host_keeps_the_host(runtime, role) -> None:
    plan = _plan(_Cfg(sandbox="host"), role=role, runtime=runtime, mode="enforce")
    assert plan.where == "host"
    assert plan.degraded is False


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_containment_scope_is_unchanged_for_agents_that_never_asked(mode) -> None:
    """The playground widens what can run; it does not containerise agents the
    legacy ladder leaves on the host. A container is `--network none`, and
    putting one round an agent that never asked would block what works today."""
    plan = _plan(_Cfg(sandbox=""), role="member", runtime=PRESENT, mode=mode)
    assert plan.where == "host"
    assert plan.degraded is False


def test_enforce_without_a_runtime_degrades_an_owner_instead_of_killing_the_run() -> None:
    plan = _plan(_Cfg(sandbox=""), role="owner", runtime=ABSENT, mode="enforce")
    assert plan.where == "host"
    assert plan.degraded is True


def test_plans_are_kept_per_run() -> None:
    plan = _plan(role="member")
    sp.set_exec_plan("run-a", plan)
    assert sp.exec_plan_for("run-a") is plan
    assert sp.exec_plan_for("run-b") is None
    assert sp.exec_plan_for("") is None


def test_a_role_that_may_run_on_the_host() -> None:
    assert sp.role_may_run_on_host("owner")
    assert sp.role_may_run_on_host("ADMIN")
    assert not sp.role_may_run_on_host("member")
    assert not sp.role_may_run_on_host("")


def test_a_sub_agents_unlock_names_the_top_level_run_not_the_role() -> None:
    """The person behind a sub-agent may already be the owner; "an owner or
    admin can run it" tells them nothing. The unlock is the parent run."""
    plan = _plan(role="owner", root=False)
    assert "top-level run" in plan.unlock
    assert "parent agent" in plan.unlock
    assert "An owner or admin can run it" not in plan.unlock
    assert "install" in plan.unlock


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"v2": {"sandbox": "docker"}}, "docker"),
        ({"v2": {"sandbox": "host"}}, "host"),
        ({"sandbox": "docker"}, "local"),
        ({}, "local"),
        ({"v2": None}, "local"),
    ],
)
def test_the_manifest_sandbox_accessor_reads_what_the_engine_reads(manifest, expected) -> None:
    assert sp.manifest_sandbox(manifest) == expected
