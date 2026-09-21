"""Planning and verification must use the owning agent's reviewed provider routes."""

import asyncio
from types import SimpleNamespace

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.provider_routing import apply_provider_order
from robothor.engine.run_lifecycle import RunLifecycleMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan", "verify"])
async def test_auxiliary_calls_keep_concurrent_agent_routes_and_clear_scope(monkeypatch, stage):
    model = "openrouter/example/model"
    seen = []

    async def provider(**kwargs):
        await asyncio.sleep(0)
        seen.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"passed": true, "confidence": 1, "plan": [], "estimated_steps": 1}'
                    ),
                    finish_reason="stop",
                )
            ]
        )

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr("robothor.engine.key_pool.api_key_for_model", lambda model: None)

    async def run(endpoint):
        config = AgentConfig(id=endpoint, name=endpoint, provider_order={model: [endpoint]})
        lifecycle = RunLifecycleMixin()
        if stage == "plan":
            return await lifecycle._run_planner(config, "Review captured evidence", [], [model])
        session = SimpleNamespace(run=SimpleNamespace(steps=[]), messages=[])
        return await lifecycle._run_verification(
            config, session, [model], [], "Completed", None, None
        )

    await asyncio.gather(run("provider-a"), run("provider-b"))
    assert len(seen) == 2
    assert {tuple(k.get("extra_body", {}).get("provider", {}).get("only", [])) for k in seen} == {
        ("provider-a",),
        ("provider-b",),
    }
    assert all(k["extra_body"]["provider"]["allow_fallbacks"] is False for k in seen)
    after = {}
    apply_provider_order(model, after)
    assert after == {}
