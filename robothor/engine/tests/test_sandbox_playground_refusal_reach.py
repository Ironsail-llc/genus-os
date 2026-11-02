"""A refused plan refuses everything that would have run in the container.

Review of the first round: with no runtime there is no container in the run's
ContextVar, so the refusal reached `exec` only. `execute_code` and the
desktop/browser tools -- the other tools a container confines -- would run on
the engine host for exactly the people the plan refuses. And a sub-agent whose
parent row was never recorded (empty `parent_run_id`) was taken for a root run
and given the host. Both are pinned here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine import sandbox_policy as sp
from robothor.engine.code_exec_guards import preflight
from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.models import TriggerType
from robothor.engine.sandbox import RuntimeStatus

ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")


@dataclass
class _Cfg:
    id: str = "example-agent"
    sandbox: str = "docker"
    tools_allowed: list[str] = field(default_factory=lambda: ["exec", "execute_code"])
    tools_denied: list[str] = field(default_factory=list)
    guardrails: list[str] = field(default_factory=list)
    guardrails_opt_out: bool = False


class _Run:
    def __init__(
        self,
        run_id: str,
        role: str,
        *,
        parent: str | None = None,
        trigger: TriggerType = TriggerType.MANUAL,
        depth: int = 0,
    ) -> None:
        self.id = run_id
        self.user_role = role
        self.parent_run_id = parent
        self.trigger_type = trigger
        self.nesting_depth = depth
        self.is_benchmark = False
        self.steps: list[Any] = []
        self.tenant_id = "default"


class _Proxy:
    def __init__(self, run: _Run) -> None:
        self.allowed = frozenset({"exec", "execute_code"})
        self._req = type("_Req", (), {"session": type("_S", (), {"run": run})()})()


@pytest.fixture(autouse=True)
def _playground(monkeypatch):
    monkeypatch.setattr("robothor.engine.sandbox.runtime_status", lambda **_k: ABSENT)
    monkeypatch.setattr(sp, "_record", lambda *_a, **_k: None)


async def _establish(run: _Run, cfg: _Cfg | None = None) -> sp.ExecPlan:
    return await sp.establish_execution_plan(
        cfg or _Cfg(), "off", agent_id="example-agent", run=run
    )


# ── execute_code ────────────────────────────────────────────────────


async def test_execute_code_is_refused_for_a_refused_plan_with_no_container() -> None:
    run = _Run("run-member-ec", "member")
    plan = await _establish(run)
    assert plan.where == "refuse"
    refusal = preflight("print(1)", _Proxy(run))
    assert refusal is not None
    assert refusal.get("error_type") == "exec_unavailable"
    assert "member" in refusal["error"]
    assert "owner or admin" in refusal.get("unlock", "")


async def test_execute_code_still_runs_for_an_owner_with_no_container() -> None:
    run = _Run("run-owner-ec", "owner")
    assert (await _establish(run)).where == "host"
    assert preflight("print(1)", _Proxy(run)) is None


# ── sub-agents whose parent row was never recorded ──────────────────


@pytest.mark.parametrize(
    ("trigger", "depth"),
    [(TriggerType.SUB_AGENT, 1), (TriggerType.SUB_AGENT, 0), (TriggerType.MANUAL, 1)],
)
async def test_a_sub_agent_with_no_parent_row_is_not_a_root_run(trigger, depth) -> None:
    run = _Run(f"run-child-{trigger}-{depth}", "owner", parent=None, trigger=trigger, depth=depth)
    plan = await _establish(run)
    assert plan.where == "refuse"
    assert "sub-agent" in plan.reason
    assert sp.proxy_may_run_on_host(_Proxy(run)) is False


# ── desktop and browser ─────────────────────────────────────────────


def _engine() -> GuardrailEngine:
    return GuardrailEngine(enabled_policies=["no_destructive_writes"])


@pytest.mark.parametrize("tool", ["desktop_click", "desktop_screenshot", "browser"])
def test_a_refused_plan_blocks_the_tools_a_container_confines(tool) -> None:
    result = _engine().check_pre_execution(
        tool, {}, agent_id="example-agent", host_refusal="no sandbox runtime; role member"
    )
    assert not result.allowed
    assert result.guardrail_name == "sandbox_playground"
    assert "no sandbox runtime" in result.reason


def test_a_refused_plan_leaves_other_tools_alone() -> None:
    result = _engine().check_pre_execution(
        "read_file", {"path": "x"}, agent_id="example-agent", host_refusal="no runtime"
    )
    assert result.allowed


async def test_admission_hands_the_refusal_to_the_guardrails() -> None:
    run = _Run("run-member-gr", "member")
    await _establish(run)
    session = type("_S", (), {"run": run})()
    kwargs = sp.guardrail_context(session)
    assert "no sandbox runtime" in kwargs.get("host_refusal", "")
    owner = _Run("run-owner-gr", "owner")
    await _establish(owner)
    assert not sp.guardrail_context(type("_S", (), {"run": owner})()).get("host_refusal")


# ── no guardrail engine to carry that block: fail closed as before ──


async def test_an_agent_without_guardrails_holding_desktop_tools_keeps_the_old_failure() -> None:
    cfg = _Cfg(tools_allowed=["exec", "desktop_click"], guardrails_opt_out=True)
    run = _Run("run-member-nogr", "member")
    decision, degrade = await sp.playground_decision(cfg, "off", "docker", "example-agent", run)
    assert decision == "docker", "nothing would gate desktop_* on the host: try the container"
    assert await degrade("no podman") is False, "and fail closed when it cannot start"


async def test_with_guardrails_the_same_agent_degrades_to_a_refusal() -> None:
    cfg = _Cfg(tools_allowed=["exec", "desktop_click"])
    run = _Run("run-member-gr2", "member")
    decision, degrade = await sp.playground_decision(cfg, "off", "docker", "example-agent", run)
    assert decision != "docker"
    assert await degrade("no podman") is True


@pytest.mark.asyncio
async def test_runner_member_run_with_no_runtime_still_completes(engine_config, mock_db) -> None:
    from unittest.mock import MagicMock

    from robothor.engine.models import AgentConfig
    from robothor.engine.runner import AgentRunner

    runner = AgentRunner(engine_config)
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = "Done"
    choice.message.tool_calls = None
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=1, completion_tokens=1)

    async def _completion(**_k: Any) -> Any:
        return response

    cfg = AgentConfig(
        id="sb2",
        name="sb2",
        model_primary="test-model",
        sandbox="docker",
        tools_allowed=["exec", "execute_code"],
    )
    with (
        patch("litellm.acompletion", side_effect=_completion),
        patch.object(runner.registry, "build_for_agent", return_value=[]),
        patch.object(runner.registry, "get_tool_names", return_value=["exec"]),
    ):
        run = await runner.execute("sb2", "hi", agent_config=cfg, user_id="u", user_role="member")
    assert run.status.value == "completed", run.error_message
    from robothor.engine.sandbox import get_current_sandbox

    assert get_current_sandbox() is None, "a refused plan must not leave a sandbox behind"
