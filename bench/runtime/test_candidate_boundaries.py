"""Adversarial synthetic proposals through installed, public framework APIs."""

from unittest.mock import patch

import pytest

from bench.runtime.candidates import DeepAgentsCandidate, FixtureGateway, PydanticCandidate

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")


def candidate(name, proposals, calls, on_request=lambda: None):
    if name == "pydantic-ai":
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel

        def response(messages, info):
            calls.append(True)
            on_request()
            return ModelResponse(parts=[ToolCallPart(tool, args) for tool, args in proposals])

        return PydanticCandidate(FunctionModel(response))

    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class CountRequests(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            calls.append(True)
            on_request()

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            assert {tool.name for tool in tools} == {"record"}
            return self

    return DeepAgentsCandidate(
        Model(
            callbacks=[CountRequests()],
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": tool, "args": args, "id": f"proposal-{index}", "type": "tool_call"}
                        for index, (tool, args) in enumerate(proposals)
                    ],
                )
            ],
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
@pytest.mark.parametrize("denial", ["stopped", "other-tenant"])
async def test_denied_admission_never_calls_a_model(name, denial):
    gateway = FixtureGateway("fixture", stopped=denial == "stopped")
    calls = []
    adapter = candidate(name, [("record", {"key": "report", "value": "delivered"})], calls)
    with pytest.raises(ValueError, match="tenant authority denied or stopped"):
        await adapter.run(gateway, tenant="other" if denial == "other-tenant" else "fixture")
    assert calls == []
    assert gateway.writes == gateway.dispatches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_stop_during_provider_work_denies_its_proposed_write(name):
    gateway, calls = FixtureGateway("fixture"), []
    adapter = candidate(
        name,
        [("record", {"key": "report", "value": "delivered"})],
        calls,
        lambda: setattr(gateway, "stopped", True),
    )
    with pytest.raises(ValueError, match="tenant authority denied or stopped"):
        await adapter.run(gateway, tenant="fixture")
    assert len(calls) == 1
    assert gateway.writes == gateway.dispatches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_unrequested_value_never_mutates_and_execution_is_bounded(name):
    from pydantic_ai.exceptions import UsageLimitExceeded

    gateway, calls = FixtureGateway("fixture"), []
    adapter = candidate(name, [("record", {"key": "unrequested", "value": "value"})], calls)
    with pytest.raises(
        UsageLimitExceeded if name == "pydantic-ai" else ValueError,
        match="request_limit of 4|model call bound exceeded",
    ):
        await adapter.run(gateway, tenant="fixture")
    assert gateway.writes == 0 and gateway.values == {}
    assert 1 <= len(calls) <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_proposed_framework_file_write_cannot_reach_backend(name, tmp_path):
    import deepagents
    from deepagents.backends import FilesystemBackend
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    original = deepagents.create_deep_agent

    def factory(*args, **kwargs):
        return original(*args, backend=FilesystemBackend(root_dir=tmp_path), **kwargs)

    gateway, calls = FixtureGateway("fixture"), []
    adapter = candidate(
        name, [("write_file", {"file_path": "/unauthorized.txt", "content": "denied"})], calls
    )
    with (
        patch("deepagents.create_deep_agent", side_effect=factory),
        pytest.raises(
            UnexpectedModelBehavior if name == "pydantic-ai" else ValueError,
            match="write_file.*exceeded max retries|framework tool bypass denied",
        ),
    ):
        await adapter.run(gateway, tenant="fixture")
    assert not list(tmp_path.iterdir())
    assert gateway.writes == gateway.dispatches == 0
    assert 1 <= len(calls) <= 4
