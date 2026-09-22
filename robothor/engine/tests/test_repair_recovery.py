import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from robothor.engine import repair_recovery
from robothor.engine.models import AgentConfig, DeliveryMode
from robothor.identity import IdentityContext


async def test_verified_repair_resumes_original_request_once(tmp_path, monkeypatch):
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
                    "task_context": {
                        "request": "Finish the original browser task",
                        "mode": "execute",
                    },
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
    coroutines = []
    monkeypatch.setattr(
        registry,
        "get_task_registry",
        lambda: SimpleNamespace(spawn=lambda coro, **kw: coroutines.append(coro)),
    )
    config = SimpleNamespace(workspace=tmp_path, manifest_dir=tmp_path)
    runner = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(id="new-run", output_text="Resumed", error_message=None)
        )
    )
    assert (await repair_recovery.resume(job_id, runner, config))["status"] == "scheduled"
    assert (await repair_recovery.resume(job_id, runner, config))["status"] == "scheduled"
    assert len(coroutines) == 1
    await coroutines[0]
    kwargs = runner.execute.await_args.kwargs
    assert "Finish the original browser task" in kwargs["message"]
    assert "do not duplicate" in kwargs["message"]
    assert kwargs["agent_config"].delivery_mode == DeliveryMode.NONE
    assert kwargs["correlation_id"] == job_id
    assert json.loads(path.read_text())["resume_run_id"] == "new-run"


async def test_plan_only_or_unverified_repairs_never_resume(tmp_path, monkeypatch):
    job_id = str(uuid.uuid4())
    path = tmp_path / "local/repairs" / job_id / "state.json"
    path.parent.mkdir(parents=True)
    for status, mode in [("tested", "execute"), ("verified", "plan")]:
        path.write_text(
            json.dumps({"status": status, "continuation": {"task_context": {"mode": mode}}})
        )
        runner = SimpleNamespace(execute=AsyncMock())
        result = await repair_recovery.resume(job_id, runner, SimpleNamespace(workspace=tmp_path))
        assert result["status"] == "no_execution_continuation"
        runner.execute.assert_not_called()
