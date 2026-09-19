"""Scoped provider tool requirements do not grant tools or escape the owning stage."""

import asyncio

import pytest

TOOLS = [{"type": "function", "function": {"name": "research"}}]


@pytest.mark.asyncio
async def test_requirement_is_scoped_pending_and_closed_for_inherited_tasks():
    from robothor.engine.required_tool import required_tool_scope, tool_choice

    pending = True
    go = asyncio.Event()

    async def inherited():
        await go.wait()
        return tool_choice(TOOLS)

    assert tool_choice(TOOLS) == "auto"
    with required_tool_scope("research", lambda: pending):
        assert tool_choice(TOOLS) == {"type": "function", "function": {"name": "research"}}
        pending = False
        assert tool_choice(TOOLS) == "auto"
        pending = True
        task = asyncio.create_task(inherited())
    go.set()
    assert await task == "auto"
    assert tool_choice(TOOLS) == "auto"


def test_requirement_cannot_add_a_missing_tool():
    from robothor.engine.required_tool import required_tool_scope, tool_choice

    with required_tool_scope("not-granted", lambda: True), pytest.raises(ValueError):
        tool_choice(TOOLS)


def test_bounded_quote_requires_endpoint_support_for_forced_tool_choice():
    from robothor.engine.request_budget import openrouter_quote
    from robothor.engine.tests.test_request_budget import endpoint

    cheap = endpoint(tag="cheap")
    supported = endpoint(
        tag="capable",
        supported_parameters=["max_tokens", "tools", "tool_choice"],
        supports_tool_choice={"function": True},
        pricing={"prompt": "0.000002", "completion": "0.000003"},
    )
    _, request = openrouter_quote(
        {
            "max_tokens": 100,
            "tools": TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "research"}},
        },
        [cheap, supported],
    )
    assert request["extra_body"]["provider"]["only"] == ["capable"]


@pytest.mark.parametrize(
    "capabilities", [None, {}, {"function": False}, {"function": "true"}, {"required": "true"}]
)
def test_general_tool_support_does_not_prove_named_function_support(capabilities):
    from robothor.engine.request_budget import openrouter_quote
    from robothor.engine.tests.test_request_budget import endpoint

    parameters = ["max_tokens", "tools", "tool_choice"]
    cheap = endpoint(
        tag="cheap", supported_parameters=parameters, supports_tool_choice=capabilities
    )
    capable = endpoint(
        tag="capable",
        supported_parameters=parameters,
        supports_tool_choice={"function": True},
        pricing={"prompt": "0.000002", "completion": "0.000003"},
    )
    _, request = openrouter_quote(
        {
            "max_tokens": 100,
            "tools": TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "research"}},
        },
        [cheap, capable],
    )
    assert request["extra_body"]["provider"]["only"] == ["capable"]


def test_required_only_endpoint_can_honor_named_tool_by_narrowing_available_schema():
    from copy import deepcopy

    from robothor.engine.request_budget import openrouter_quote
    from robothor.engine.tests.test_request_budget import endpoint

    kwargs = {
        "max_tokens": 100,
        "tools": TOOLS + [{"type": "function", "function": {"name": "other"}}],
        "tool_choice": {"type": "function", "function": {"name": "research"}},
    }
    original = deepcopy(kwargs)
    supported = endpoint(
        supported_parameters=["max_tokens", "tools", "tool_choice"],
        supports_tool_choice={"function": False, "required": True},
    )
    _, request = openrouter_quote(kwargs, [supported])
    assert request["tool_choice"] == "required"
    assert request["tools"] == TOOLS
    assert kwargs == original


@pytest.mark.parametrize(
    "tools", [[], [{"type": "function", "function": {"name": "other"}}], TOOLS * 2]
)
def test_required_emulation_never_adds_missing_or_ambiguous_named_tool(tools):
    from robothor.engine.request_budget import RequestBudgetError, openrouter_quote
    from robothor.engine.tests.test_request_budget import endpoint

    with pytest.raises(RequestBudgetError):
        openrouter_quote(
            {
                "max_tokens": 100,
                "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": "research"}},
            },
            [
                endpoint(
                    supported_parameters=["max_tokens", "tools", "tool_choice"],
                    supports_tool_choice={"function": False, "required": True},
                )
            ],
        )
