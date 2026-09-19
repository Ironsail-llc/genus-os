"""Agent JSON mode survives dispatch and does not leak between concurrent runs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.config import manifest_to_agent_config
from robothor.engine.llm_client import LLMClient
from robothor.sales.models import Dossier


def test_manifest_preserves_the_declared_response_mode():
    config = manifest_to_agent_config(
        {"id": "example", "model": {"response_format": "json_object"}}
    )
    assert config.response_format == "json_object"
    assert manifest_to_agent_config({"id": "example"}).response_format == "text"


def test_dossier_schema_teaches_nonempty_criterion_references():
    criteria = Dossier.model_json_schema()["properties"]["criteria"]["additionalProperties"]
    assert criteria["minItems"] == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_json_mode_is_scoped_to_the_call_and_shared_by_request_builders(
    monkeypatch, streaming
):
    client = LLMClient()
    seen = []

    async def dispatch(messages, *args, **kwargs):
        await asyncio.sleep(0)
        request = client._build_llm_kwargs(
            "openrouter/example/model", messages, [], 100, 0.3, stream=streaming
        )
        seen.append((messages[0]["content"], request))
        return object()

    monkeypatch.setattr(client, "_call_llm", dispatch)
    monkeypatch.setattr(client, "_call_llm_streaming", dispatch)

    def session(mode):
        return SimpleNamespace(
            response_format=mode,
            messages=[{"role": "user", "content": mode}],
            run=SimpleNamespace(id=mode, trigger_type="sub_agent"),
        )

    await asyncio.gather(
        *[
            client._do_llm_call(
                session(mode),
                ["openrouter/example/model"],
                [],
                (lambda _: None) if streaming else None,
                set(),
                0.3,
            )
            for mode in ("json_object", "text")
        ]
    )
    requests = dict(seen)
    assert requests["json_object"]["response_format"] == {"type": "json_object"}
    assert "response_format" not in requests["text"]
    assert "response_format" not in client._build_llm_kwargs(
        "openrouter/example/model", [], [], 100, 0.3
    )


async def test_response_mode_restored_after_dispatch_error(monkeypatch):
    client = LLMClient()
    monkeypatch.setattr(
        client, "_call_llm", AsyncMock(side_effect=RuntimeError("provider unavailable"))
    )
    session = SimpleNamespace(
        response_format="json_object",
        messages=[],
        run=SimpleNamespace(id="run", trigger_type="sub_agent"),
    )
    with pytest.raises(RuntimeError):
        await client._do_llm_call(session, ["openrouter/example/model"], [], None, set(), 0.3)
    assert "response_format" not in client._build_llm_kwargs(
        "openrouter/example/model", [], [], 100, 0.3
    )
