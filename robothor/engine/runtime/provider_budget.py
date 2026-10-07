"""Conservative text request reservations shared by all runs in a goal attempt.

The input bound is a TOKEN ceiling — the engine's ceiling-grade estimator plus a
per-message framing allowance — because the shared budget it is subtracted from
is denominated in tokens. Images, audio and provider-side tools have no bound
here and are refused for capped goals. Each real provider attempt gets its own
reservation; an uncertain attempt stays charged.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from robothor.engine.request_budget import RequestBudgetError


class DurableStopError(RequestBudgetError):
    """A committed operator control denied this provider admission."""


def usage_tokens(response: Any) -> int | None:
    usage = (
        response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    )
    value = (
        usage.get("total_tokens")
        if isinstance(usage, dict)
        else getattr(usage, "total_tokens", None)
    )
    return value if type(value) is int and value >= 0 else None


async def budget_value(budget: Any, name: str) -> Any:
    asynchronous = getattr(budget, "value_async", None)
    return await asynchronous(name) if asynchronous else getattr(budget, name)


async def budget_operation(budget: Any, name: str, *args: Any) -> None:
    asynchronous = getattr(budget, name + "_async", None)
    if asynchronous:
        await asynchronous(*args)
    else:
        getattr(budget, name)(*args)


#: Framing allowance per message, plus one for the request envelope. It is in
#: TOKENS, like everything else subtracted from the budget. The value was 256
#: BYTES per message back when the whole bound was measured in bytes; keeping
#: the number keeps the allowance conservative rather than quietly shrinking it.
_FRAMING_TOKENS = 256


def _input_token_bound(kwargs: dict[str, Any]) -> int:
    """A ceiling on this request's INPUT tokens, in the budget's own unit.

    The shared budget is denominated in tokens — ``SharedBudget(token_remaining)``,
    where ``token_remaining`` is the goal's token budget less what it has spent,
    and it is settled against the provider's reported ``total_tokens``. The bound
    subtracted from it therefore has to be tokens too.

    It used to be the payload's UTF-8 BYTE count. Bytes are a valid upper bound on
    tokens, but three to four times larger than the number they stood in for, so
    the subtraction over-charged every request: on 2026-09-22 a goal with 72,200
    tokens left refused a request whose payload was 68,104 bytes — roughly 20,000
    tokens, which it could easily afford — and the pursuit stopped with the budget
    unspent.

    ``context_fit.estimate_for`` is the engine's ceiling-grade estimator: it prices
    dense content at what it costs and errs high, which is what a reservation
    needs. Tools and the response format are prompt too, so they are priced
    through the same arithmetic rather than a second, disagreeing one.
    """
    from robothor.engine.context_fit import estimate_for

    messages = kwargs.get("messages", [])
    model = kwargs.get("model")
    bound = estimate_for(messages, model) + _FRAMING_TOKENS * (len(messages) + 1)
    extra = {key: kwargs[key] for key in ("tools", "response_format") if key in kwargs}
    if extra:
        bound += estimate_for(
            [{"role": "system", "content": json.dumps(extra, ensure_ascii=False)}], model
        )
    return bound


async def goal_completion(call: Any, kwargs: dict[str, Any], budget: Any) -> Any:
    from robothor.engine.request_budget import _text_only

    if (
        not _text_only(kwargs.get("messages", []))
        or any(tool.get("type") != "function" for tool in kwargs.get("tools", []))
        or kwargs.get("n", 1) != 1
    ):
        raise RequestBudgetError("No goal token bound for multimodal or provider-tool requests")
    input_bound = _input_token_bound(kwargs)
    limit = await budget_value(budget, "limit")
    remaining = limit - await budget_value(budget, "charged") if limit is not None else None
    output_bound = kwargs.get("max_tokens") or 4096
    if remaining is not None:
        output_bound = min(output_bound, remaining - input_bound)
    if output_bound <= 0:
        raise RequestBudgetError("Goal budget cannot cover the request's conservative input bound")
    call_id = str(uuid4())
    try:
        await budget_operation(budget, "reserve", call_id, input_bound + output_bound)
    except ValueError as exc:
        raise RequestBudgetError(str(exc)) from exc
    import asyncio

    from robothor.goals.runtime import binding
    from robothor.goals.store import reserve_provider_usage

    current = binding.get()
    if current:
        await asyncio.to_thread(
            reserve_provider_usage,
            current.tenant,
            current.goal_id,
            current.attempt,
            await budget_value(budget, "charged"),
        )
    response = await call(**{**kwargs, "max_tokens": output_bound})
    if kwargs.get("stream"):
        return TokenStream(response, budget, call_id)
    await budget_operation(budget, "settle", call_id, usage_tokens(response))
    return response


class TokenStream:
    def __init__(self, source: Any, budget: Any, call_id: str) -> None:
        self.source, self.budget, self.call_id = source, budget, call_id
        self.iterator = source.__aiter__()
        self.actual: int | None = None
        self.done = False

    def __aiter__(self) -> TokenStream:
        return self

    async def __anext__(self) -> Any:
        if self.done:
            raise StopAsyncIteration
        try:
            chunk = await self.iterator.__anext__()
        except StopAsyncIteration:
            self.done = True
            await budget_operation(self.budget, "settle", self.call_id, self.actual)
            raise
        except BaseException:
            await self.aclose()
            raise
        tokens = usage_tokens(chunk)
        if tokens is not None:
            self.actual = tokens
        return chunk

    async def aclose(self) -> None:
        self.done = True
        close = getattr(self.source, "aclose", None)
        if close:
            await close()


async def assert_provider_authorized() -> None:
    """A committed stop also prevents a later provider attempt or fallback."""
    import asyncio

    from robothor.engine.runtime.activity import current
    from robothor.engine.runtime.controls import stopped
    from robothor.engine.runtime.current import active_context
    from robothor.engine.runtime.deadlines import require_time

    require_time()
    from robothor.engine.resume_claim import current as resume_claim
    from robothor.engine.resume_claim import require_owned

    if resume_claim.get() is not None:
        await asyncio.to_thread(require_owned)
    activity = current.get()
    context = active_context.get()
    if activity and activity.sessions:
        session = next(iter(activity.sessions.values()))
        if await asyncio.to_thread(stopped, session.run.tenant_id, session.run_id):
            raise DurableStopError("Durable stop denies another provider request")
    elif context is not None:
        # `context and await ...` made the branch's type a union of the context
        # and the bool, which is not what the check means. It also leaned on a
        # frozen dataclass being truthy, which is always true.
        if await asyncio.to_thread(stopped, context.tenant_id, ""):
            raise DurableStopError("Durable stop denies another provider request")
