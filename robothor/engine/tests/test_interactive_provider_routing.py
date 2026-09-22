"""Bounded chat routing preserves explicit choices and execution boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from robothor.engine.llm_client import LLMClient
from robothor.engine.provider_routing import apply_provider_order, provider_order_scope
from robothor.engine.runtime import ExecutionContext, RunRequest
from robothor.engine.runtime.classification_window import guard, release
from robothor.engine.runtime.classified_deadline import admission
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.interactive_routing import prefer_throughput

MODEL = "openrouter/example/model"


def request():
    config = SimpleNamespace(
        difficulty_class="", planning_enabled=False, planning_model="", is_benchmark=False
    )
    return RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Create a task and calculate 17 times 19",
        {"trigger_type": "webchat", "agent_config": config},
    )


def session(req):
    return SimpleNamespace(
        run=SimpleNamespace(
            correlation_id=req.context.request_id,
            tenant_id=req.context.tenant_id,
            trigger_type=req.options["trigger_type"],
        ),
        messages=[],
        provider_order={},
    )


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("trigger", ["webchat", "telegram"])
async def test_native_dispatch_applies_preference_before_kwargs_leave_client(
    monkeypatch, streaming, trigger
):
    req = request()
    req.options["trigger_type"] = trigger
    client = LLMClient()
    seen = []

    async def dispatch(*args, **kwargs):
        seen.append(client._build_llm_kwargs(MODEL, [], [], 100, 0.3, stream=streaming))
        return object()

    monkeypatch.setattr(client, "_call_llm_streaming", dispatch)
    monkeypatch.setattr(client, "_call_llm", dispatch)
    token = active_context.set(req.context)
    try:
        with admission(req):
            async with guard(req):
                await client._do_llm_call(
                    session(req), [MODEL], [], (lambda _: None) if streaming else None, set(), 0.3
                )
    finally:
        active_context.reset(token)
    assert seen[0].get("extra_body", {}).get("provider", {}).get("sort") == "throughput"
    assert "provider" not in client._build_llm_kwargs(MODEL, [], [], 100, 0.3).get("extra_body", {})


@pytest.mark.parametrize(
    "variant",
    [
        "goal",
        "child",
        "resume",
        "readonly_mode",
        "deep_plan",
        "spawn_context",
        "cron",
        "planning_enabled",
        "planning_model",
        "moderate",
        "complex",
        "is_benchmark",
        "approved_plan",
        "explicit_deadline",
        "foreign_context",
        "foreign_session",
        "released",
    ],
)
async def test_unqualified_work_does_not_inherit_preference(variant):
    req = request()
    config = req.options["agent_config"]
    if variant == "goal":
        req = replace(req, context=replace(req.context, goal_id="goal", attempt_id="attempt"))
    elif variant == "child":
        req = replace(req, context=replace(req.context, parent_id="parent"))
    elif variant == "resume":
        req = replace(req, resume_from="saved-run")
    elif variant == "cron":
        req.options["trigger_type"] = "cron"
    elif variant in {"planning_enabled", "is_benchmark"}:
        setattr(config, variant, True)
    elif variant == "planning_model":
        config.planning_model = MODEL
    elif variant in {"moderate", "complex"}:
        config.difficulty_class = variant
    elif variant == "approved_plan":
        req.options["trigger_detail"] = "plan:approved"
    elif variant == "explicit_deadline":
        req = replace(
            req, context=replace(req.context, deadline=datetime.now(UTC) + timedelta(seconds=5))
        )
    elif variant in {"readonly_mode", "deep_plan", "spawn_context"}:
        req.options[variant] = True
    current = (
        replace(req.context, principal_id="other") if variant == "foreign_context" else req.context
    )
    native = session(req)
    if variant == "foreign_session":
        native.run.correlation_id = "other"
    token = active_context.set(current)
    try:
        with admission(req):
            async with guard(req):
                if variant == "released":
                    release()
                assert not prefer_throughput(native)
    finally:
        active_context.reset(token)


async def test_explicit_simple_bound_remains_eligible():
    req = request()
    req.options["agent_config"].difficulty_class = "simple"
    req = replace(
        req, context=replace(req.context, deadline=datetime.now(UTC) + timedelta(seconds=20))
    )
    token = active_context.set(req.context)
    try:
        with admission(req):
            async with guard(req):
                assert prefer_throughput(session(req))
    finally:
        active_context.reset(token)


@pytest.mark.parametrize("explicit", [{}, {"order": ["chosen"]}, {"sort": "price"}])
def test_routing_preserves_constraints_and_explicit_preferences(explicit):
    original = {
        "only": ["eligible"],
        "max_price": {"prompt": 1},
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": False,
        **explicit,
    }
    kwargs = {"extra_body": {"provider": original, "reasoning": {"effort": "medium"}}}
    with provider_order_scope({}, prefer_throughput=True):
        apply_provider_order(MODEL, kwargs)
    assert kwargs["extra_body"]["provider"] == (
        original if explicit else {**original, "sort": "throughput"}
    )
    assert "sort" not in original or original["sort"] == "price"
    assert kwargs["extra_body"]["reasoning"] == {"effort": "medium"}


def test_manifest_compatibility_and_local_routes_win():
    with provider_order_scope({MODEL: ["chosen"]}, prefer_throughput=True):
        assert LLMClient._build_llm_kwargs(MODEL, [], [], 100, 0.3)["extra_body"]["provider"] == {
            "only": ["chosen"],
            "order": ["chosen"],
            "allow_fallbacks": False,
        }
        assert (
            "sort"
            not in LLMClient._build_llm_kwargs("openrouter/anthropic/example", [], [], 100, 0.3)[
                "extra_body"
            ]["provider"]
        )
        local = {}
        apply_provider_order("ollama_chat/example", local)
        assert local == {}


async def test_closed_scope_does_not_leak_to_inherited_task():
    ready = asyncio.Event()

    async def child():
        await ready.wait()
        kwargs = {}
        apply_provider_order(MODEL, kwargs)
        return kwargs

    with provider_order_scope({}, prefer_throughput=True):
        task = asyncio.create_task(child())
    ready.set()
    assert await task == {}


def test_budget_quote_still_pins_and_reserves_an_eligible_endpoint():
    from robothor.engine.request_budget import openrouter_quote
    from robothor.engine.tests.test_request_budget import endpoint

    kwargs = {"max_tokens": 100, "extra_body": {"provider": {"only": ["allowed"]}}}
    candidates = [endpoint(tag="allowed"), endpoint(tag="other")]
    expected_units, expected = openrouter_quote(kwargs, candidates)
    with provider_order_scope({}, prefer_throughput=True):
        apply_provider_order(MODEL, kwargs)
    units, bounded = openrouter_quote(kwargs, candidates)
    assert units == expected_units
    assert bounded["extra_body"]["provider"] == {
        **expected["extra_body"]["provider"],
        "sort": "throughput",
    }
    assert bounded["num_retries"] == 0
