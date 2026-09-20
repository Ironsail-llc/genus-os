"""Conservative text request reservations shared by all runs in a goal attempt.

UTF-8 payload bytes plus message framing bound supported text tokenizers. Images,
audio and provider-side tools have no bound here and are refused for capped goals.
Each real provider attempt gets its own reservation; an uncertain attempt stays charged.
"""

from __future__ import annotations

import json
from uuid import uuid4

from robothor.engine.request_budget import RequestBudgetError


def usage_tokens(response):
    usage = (
        response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    )
    value = (
        usage.get("total_tokens")
        if isinstance(usage, dict)
        else getattr(usage, "total_tokens", None)
    )
    return value if type(value) is int and value >= 0 else None


async def goal_completion(call, kwargs, budget):
    from robothor.engine.request_budget import _text_only

    if (
        not _text_only(kwargs.get("messages", []))
        or any(tool.get("type") != "function" for tool in kwargs.get("tools", []))
        or kwargs.get("n", 1) != 1
    ):
        raise RequestBudgetError("No goal token bound for multimodal or provider-tool requests")
    payload = {
        key: kwargs[key] for key in ("messages", "tools", "response_format") if key in kwargs
    }
    input_bound = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256 * (
        len(kwargs.get("messages", [])) + 1
    )
    remaining = budget.limit - budget.charged
    output_bound = min(kwargs.get("max_tokens") or 4096, remaining - input_bound)
    if output_bound <= 0:
        raise RequestBudgetError("Goal budget cannot cover the request's conservative input bound")
    call_id = str(uuid4())
    try:
        budget.reserve(call_id, input_bound + output_bound)
    except ValueError as exc:
        raise RequestBudgetError(str(exc)) from exc
    import asyncio

    from robothor.goals.runtime import binding
    from robothor.goals.store import reserve_provider_usage

    current = binding.get()
    if current:
        await asyncio.to_thread(
            reserve_provider_usage, current.tenant, current.goal_id, current.attempt, budget.charged
        )
    response = await call(**{**kwargs, "max_tokens": output_bound})
    if kwargs.get("stream"):
        return TokenStream(response, budget, call_id)
    budget.settle(call_id, usage_tokens(response))
    return response


class TokenStream:
    def __init__(self, source, budget, call_id):
        self.source, self.budget, self.call_id = source, budget, call_id
        self.iterator = source.__aiter__()
        self.actual = None
        self.done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.done:
            raise StopAsyncIteration
        try:
            chunk = await self.iterator.__anext__()
        except StopAsyncIteration:
            self.done = True
            self.budget.settle(self.call_id, self.actual)
            raise
        except BaseException:
            await self.aclose()
            raise
        tokens = usage_tokens(chunk)
        if tokens is not None:
            self.actual = tokens
        return chunk

    async def aclose(self):
        self.done = True
        close = getattr(self.source, "aclose", None)
        if close:
            await close()


async def assert_provider_authorized():
    """A committed stop also prevents a later provider attempt or fallback."""
    import asyncio

    from robothor.engine.runtime.activity import current
    from robothor.engine.runtime.controls import stopped

    activity = current.get()
    if activity and activity.sessions:
        session = next(iter(activity.sessions.values()))
        if await asyncio.to_thread(stopped, session.run.tenant_id, session.run_id):
            raise RequestBudgetError("Durable stop denies another provider request")
