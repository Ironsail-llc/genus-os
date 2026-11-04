"""A repair-resumed run has no person at the keyboard.

``repair_recovery.resume`` continues an owner's task after a verified repair.
It re-checks that the owner's identity still authorises the task and starts
the run as MANUAL with ``user_role="owner"``, but nobody is driving it: it
starts on its own once the repair verifies. The playground's widenings
(the exec allowlist bypass, the host fallback, execute_code on the host) are
for the person driving a run, so a repair-resumed run gets none of them.
The run is built from the kwargs ``resume`` really passes, so a change to how
it starts the run is caught here.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.engine import repair_recovery
from robothor.engine import sandbox_policy as sp
from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.models import AgentConfig
from robothor.engine.sandbox import RuntimeStatus
from robothor.identity import IdentityContext

ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")


async def _resume_kwargs(tmp_path, monkeypatch) -> dict[str, Any]:
    import robothor.engine.config as config_module
    import robothor.engine.task_registry as registry
    import robothor.identity as identity_module

    job_id = str(uuid.uuid4())
    path = tmp_path / "local/repairs" / job_id / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "status": "verified",
                "continuation": {
                    "user_id": "owner",
                    "tenant_id": "test",
                    "identity": {"channel": "telegram", "identifier": "123"},
                    "task_context": {"request": "Finish the task", "mode": "execute"},
                },
            }
        )
    )
    monkeypatch.setattr(repair_recovery, "existing_resume", lambda job: None)
    monkeypatch.setattr(
        identity_module,
        "resolve_identity",
        lambda *a, **k: IdentityContext(
            tenant_id="test", channel="telegram", identifier="123", verified=True, role="owner"
        ),
    )
    monkeypatch.setattr(
        config_module, "load_agent_config", lambda *a: AgentConfig(id="main", name="Main")
    )
    coroutines: list[Any] = []
    monkeypatch.setattr(
        registry,
        "get_task_registry",
        lambda: SimpleNamespace(spawn=lambda coro, **kw: coroutines.append(coro)),
    )
    runner = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(id="new-run", output_text="", error_message=None)
        )
    )
    config = SimpleNamespace(workspace=tmp_path, manifest_dir=tmp_path)
    assert (await repair_recovery.resume(job_id, runner, config))["status"] == "scheduled"
    await coroutines[0]
    return runner.execute.await_args.kwargs


@pytest.fixture
async def resumed_run(tmp_path, monkeypatch) -> Any:
    kwargs = await _resume_kwargs(tmp_path, monkeypatch)
    return SimpleNamespace(
        id="repair-resumed-run",
        user_role=kwargs["user_role"],
        user_id=kwargs["user_id"],
        trigger_type=kwargs["trigger_type"],
        trigger_detail=kwargs["trigger_detail"],
        nesting_depth=0,
        parent_run_id=None,
        is_benchmark=False,
        steps=[],
    )


def test_a_repair_resumed_run_has_no_acting_role(resumed_run) -> None:
    assert resumed_run.user_role == "owner"  # what resume stamps; not who drives
    assert sp.acting_host_role(resumed_run) == ""


def test_a_repair_resumed_run_keeps_the_exec_allowlist(resumed_run) -> None:
    engine = GuardrailEngine(
        enabled_policies=["exec_allowlist"],
        _exec_allowlists={"main": [re.compile(r"^git status$")]},
    )
    kwargs = sp.guardrail_context(SimpleNamespace(run=resumed_run))
    result = engine.check_pre_execution(
        "exec", {"command": "curl example.com"}, agent_id="main", **kwargs
    )
    assert not result.allowed
    assert result.guardrail_name == "exec_allowlist"


async def test_a_repair_resumed_run_gets_no_host_fallback(resumed_run, monkeypatch) -> None:
    monkeypatch.setattr("robothor.engine.sandbox.runtime_status", lambda **_k: ABSENT)
    monkeypatch.setattr(sp, "_record", lambda *_a, **_k: None)
    cfg = SimpleNamespace(id="main", sandbox="docker", tools_allowed=["exec"], tools_denied=[])
    plan = await sp.establish_execution_plan(cfg, "off", agent_id="main", run=resumed_run)
    assert plan.where == "refuse"
    assert plan.degraded is False


def test_a_repair_resumed_run_may_not_run_execute_code_on_the_host(resumed_run) -> None:
    proxy = SimpleNamespace(_req=SimpleNamespace(session=SimpleNamespace(run=resumed_run)))
    assert sp.proxy_may_run_on_host(proxy) is False
