"""Trusted workflow schemas constrain final content after required tool work."""

import asyncio

import pytest

from robothor.engine.llm_client import LLMClient
from robothor.engine.request_budget import RequestBudgetError, openrouter_quote
from robothor.engine.tests.test_request_budget import endpoint

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("schema_ready", [False, True])
def test_required_tool_turn_does_not_compete_with_final_answer_format(schema_ready):
    from robothor.engine.llm_client import _response_format_var
    from robothor.engine.required_tool import required_tool_scope
    from robothor.engine.response_schema import response_schema_scope

    tools = [{"type": "function", "function": {"name": "web_fetch", "parameters": {}}}]
    pending = True
    token = _response_format_var.set("json_object")
    try:
        with (
            response_schema_scope("result", SCHEMA, ready=lambda: schema_ready),
            required_tool_scope("web_fetch", lambda: pending),
        ):

            def request():
                return LLMClient._build_llm_kwargs("openrouter/example/model", [], tools, 100, 0.3)

            forced = request()
            assert forced["tool_choice"] == {"type": "function", "function": {"name": "web_fetch"}}
            assert "response_format" not in forced
            assert forced["messages"] == []
            pending = False
            answer = request()
            assert answer["tool_choice"] == "auto"
            assert answer["response_format"]["type"] == (
                "json_schema" if schema_ready else "json_object"
            )
    finally:
        _response_format_var.reset(token)


async def test_schema_is_ready_scoped_copied_and_closed_for_inherited_tasks():
    from robothor.engine.response_schema import response_schema_scope

    ready = False
    release = asyncio.Event()

    def request():
        return LLMClient._build_llm_kwargs("openrouter/example/model", [], [], 100, 0.3)

    async def inherited():
        await release.wait()
        return request()

    with response_schema_scope("result", SCHEMA, ready=lambda: ready):
        assert "response_format" not in request()
        ready = True
        response_format = request()["response_format"]
        assert response_format == {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": SCHEMA},
        }
        response_format["json_schema"]["schema"]["required"].clear()
        assert request()["response_format"]["json_schema"]["schema"]["required"] == ["summary"]
        task = asyncio.create_task(inherited())
    release.set()
    assert "response_format" not in (await task)


def test_structured_output_requires_endpoint_schema_support_before_funding():
    request = {
        "max_tokens": 100,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": SCHEMA},
        },
    }
    json_only = endpoint(tag="json-only", supported_parameters=["max_tokens", "response_format"])
    structured = endpoint(
        tag="structured",
        supported_parameters=["max_tokens", "response_format", "structured_outputs"],
    )
    _, bounded = openrouter_quote(request, [json_only, structured])
    assert bounded["extra_body"]["provider"]["only"] == ["structured"]
    with pytest.raises(RequestBudgetError):
        openrouter_quote(request, [json_only])
