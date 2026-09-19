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
