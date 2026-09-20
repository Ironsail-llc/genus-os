"""Opt-in pre-request spending admission shared by an entire async run.

Unlike the runner's cost forecast, this reserves a provider-constrained worst
case before dispatch. Unknown usage stays charged. No budget scope means the
existing engine behavior is unchanged. This first policy supports text-only
OpenRouter calls; unsupported billable features fail closed.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from robothor.engine.request_routes import RequestRoutes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator


class RequestBudgetError(RuntimeError):
    """The request cannot be safely admitted within this run's allowance."""


class RequestRouteUnavailableError(RequestBudgetError):
    """No eligible endpoint for this model; a separately funded fallback may run."""


_ACTIVE: ContextVar[RequestBudget | None] = ContextVar("request_budget", default=None)
_MICRO = Decimal(1_000_000)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Not a cost")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Not a cost") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Not a finite nonnegative cost")
    return result


def _cost_units(response: Any) -> int | None:
    usage = (
        response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    )
    value = usage.get("cost") if isinstance(usage, dict) else getattr(usage, "cost", None)
    if value is None:
        # LiteLLM's OpenRouter adapter copies the provider's usage.cost here
        # for non-streaming responses. Do not use response_cost: that can be
        # a local model-price estimate rather than a provider-reported charge.
        hidden = (
            response.get("_hidden_params")
            if isinstance(response, dict)
            else getattr(response, "_hidden_params", None)
        )
        headers = hidden.get("additional_headers") if isinstance(hidden, dict) else None
        if isinstance(headers, dict):
            value = headers.get("llm_provider-x-litellm-response-cost")
    try:
        return int((_decimal(value) * _MICRO).to_integral_value(rounding=ROUND_CEILING))
    except ValueError:
        return None


class RequestBudget:
    """One funded run envelope, inherited by async children and to_thread work."""

    def __init__(
        self,
        limit_units: int,
        *,
        quote: Callable[[dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]] | None = None,
    ) -> None:
        if type(limit_units) is not int or limit_units < 0:
            raise ValueError("Nonnegative integer micro-USD required")
        self.limit_units = limit_units
        self.quote = quote or OpenRouterQuotes()
        self.routes = RequestRoutes()
        self._charged = 0
        self._closed = False
        self._lock = threading.Lock()

    @property
    def charged_units(self) -> int:
        with self._lock:
            return self._charged

    def reserve(self, units: int) -> None:
        if type(units) is not int or units < 0:
            raise RequestBudgetError("Invalid request cost bound")
        with self._lock:
            if self._closed or self._charged + units > self.limit_units:
                raise RequestBudgetError("Request spending allowance exhausted or closed")
            self._charged += units

    def settle(self, reserved: int, actual: int | None) -> None:
        with self._lock:
            if actual is not None and actual > reserved:
                # Never hide a provider contract violation behind the cap.
                self._charged += actual - reserved
                self._closed = True
                raise RequestBudgetError("Provider charge exceeded the reserved request bound")
            if actual is not None and not self._closed:
                self._charged -= reserved - actual

    def close(self) -> None:
        with self._lock:
            self._closed = True


def active_budget() -> RequestBudget | None:
    """Allow non-LLM paid adapters to reject unpriced work in a bounded run."""
    return _ACTIVE.get()


@contextmanager
def budget_scope(budget: RequestBudget) -> Iterator[RequestBudget]:
    """Scope one funded envelope. Nested scopes cannot reset an existing cap."""
    if active_budget() is not None:
        raise RequestBudgetError("A funded budget scope is already active")
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        budget.close()
        _ACTIVE.reset(token)


async def bounded_completion(call: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
    """Wrap each actual provider attempt, not the caller's whole retry loop."""
    budget = active_budget()
    if budget is None:
        return await call(**kwargs)
    units, bounded = await budget.quote(budget.routes.for_quote(kwargs))
    budget.reserve(units)
    # Failures/cancellation leave the reservation fully charged: the request
    # could have reached the provider even if no response reached this process.
    route = budget.routes.identity(bounded)
    try:
        response = await call(**bounded)
    except BaseException as exc:
        budget.routes.failed(route, exc)
        raise
    if bounded.get("stream"):
        return _BudgetStream(response, budget, units, route)
    budget.settle(units, _cost_units(response))
    return response


class _BudgetStream:
    def __init__(
        self,
        source: Any,
        budget: RequestBudget,
        units: int,
        route: tuple[str, str] | None = None,
    ) -> None:
        self.route = route
        self.source = source
        self.iterator = source.__aiter__()
        self.budget = budget
        self.units = units
        self.actual: int | None = None
        self.done = False

    def __aiter__(self) -> _BudgetStream:
        return self

    async def __anext__(self) -> Any:
        if self.done:
            raise StopAsyncIteration
        try:
            chunk = await self.iterator.__anext__()
        except StopAsyncIteration:
            self.done = True
            try:
                self.budget.settle(self.units, self.actual)
            finally:
                await self._close_source()
            raise
        except BaseException as exc:
            self.budget.routes.failed(self.route, exc)
            self.done = True
            await self._close_source()
            raise
        cost = _cost_units(chunk)
        self.actual = cost
        return chunk

    async def _close_source(self) -> None:
        close = getattr(self.source, "aclose", None)
        if close is not None:
            await close()

    async def aclose(self) -> None:
        if not self.done:
            self.done = True
            await self._close_source()


def _text_only(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            if any(block.get("type") != "text" or "cache_control" in block for block in content):
                return False
        elif content is not None and not isinstance(content, str):
            return False
    return True


def openrouter_quote(
    kwargs: dict[str, Any], endpoints: list[dict[str, Any]]
) -> tuple[int, dict[str, Any]]:
    """Pin one published endpoint and reserve its FULL input context.

    No tokenizer estimate or registry price is a spending authorization. The
    request's price filter, one endpoint, disabled fallback/retries, text-only
    payload and output ceiling bound billing under the provider's API contract.
    A provider violating that contract stops the envelope and records overrun.
    """
    allowed = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "response_format",
        "temperature",
        "max_tokens",
        "stream",
        "timeout",
        "api_key",
        "api_base",
        "top_p",
        "top_k",
        "stop",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "reasoning_effort",
        "reasoning",
        "thinking",
        "extra_body",
        "n",
        "num_retries",
        "stream_options",
    }
    if (
        set(kwargs) - allowed
        or kwargs.get("n", 1) != 1
        or not _text_only(kwargs.get("messages", []))
    ):
        raise RequestBudgetError("Request includes unpriced features")
    if any(tool.get("type") != "function" for tool in kwargs.get("tools", [])):
        raise RequestBudgetError("Provider-side paid tools require a separate cost contract")
    extra = deepcopy(kwargs.get("extra_body") or {})
    if set(extra) - {"provider", "reasoning"}:
        raise RequestBudgetError("Request includes unpriced provider features")
    routing = extra.get("provider") or {}
    if set(routing) - {
        "only",
        "order",
        "ignore",
        "allow_fallbacks",
        "require_parameters",
        "max_price",
        "sort",
        "zdr",
        "data_collection",
        "quantizations",
    }:
        raise RequestBudgetError("Unsupported provider routing in bounded request")
    requested = kwargs.get("max_tokens", 4096)
    if type(requested) is not int or requested < 1:
        raise RequestBudgetError("A positive output ceiling is required")
    choices = []
    for endpoint in endpoints:
        try:
            tag = endpoint["tag"]
            if not isinstance(tag, str) or not tag:
                continue

            def matches(values: Any, tag: str = tag) -> bool:
                return any(
                    tag.lower() == str(v).lower() or tag.lower().startswith(str(v).lower() + "/")
                    for v in values
                )

            if any(routing.get(k) and not matches(routing[k]) for k in ("only", "order")):
                continue
            if matches(routing.get("ignore", [])):
                continue
            if (
                routing.get("quantizations")
                and endpoint.get("quantization") not in routing["quantizations"]
            ):
                continue
            context = endpoint["context_length"]
            maximum = endpoint["max_completion_tokens"]
            if type(context) is not int or context < 1 or type(maximum) is not int or maximum < 1:
                continue
            if requested > maximum or "max_tokens" not in endpoint["supported_parameters"]:
                continue
            # Pinning an endpoint that cannot honor JSON mode makes the
            # provider's require_parameters gate reject an otherwise valid
            # model. Filter before reserving money or attempting that route.
            if (
                kwargs.get("response_format") is not None
                and "response_format" not in endpoint["supported_parameters"]
            ):
                continue
            if (kwargs.get("response_format") or {}).get(
                "type"
            ) == "json_schema" and "structured_outputs" not in endpoint["supported_parameters"]:
                continue
            from robothor.engine.required_tool import endpoint_tool_contract

            tool_contract = endpoint_tool_contract(kwargs, endpoint)
            if tool_contract is None:
                continue
            price = endpoint["pricing"]
            # Tiered/time-varying and explicit cache-write pricing need their
            # own reviewed quote policy. Do not guess the maximum surcharge.
            if price.get("overrides") or _decimal(price.get("input_cache_write", 0)) > 0:
                continue
            known_prices = {
                "prompt",
                "completion",
                "request",
                "input_cache_read",
                "input_cache_write",
                "discount",
            }
            if set(price) - known_prices:
                continue
            prompt, completion, request = (
                _decimal(price.get(k, 0)) for k in ("prompt", "completion", "request")
            )
            if (
                "prompt" not in price
                or "completion" not in price
                or _decimal(price.get("input_cache_read", 0)) > prompt
            ):
                continue
            ceiling = {
                "prompt": prompt * _MICRO,
                "completion": completion * _MICRO,
                "request": request,
                "image": Decimal(0),
            }
            if any(
                k in routing.get("max_price", {}) and v > _decimal(routing["max_price"][k])
                for k, v in ceiling.items()
            ):
                continue
            units = int(
                ((context * prompt + requested * completion + request) * _MICRO).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            choices.append((units, tag, ceiling, tool_contract))
        except (KeyError, TypeError, ValueError):
            continue
    if not choices:
        raise RequestRouteUnavailableError(
            "No published endpoint satisfies the bounded request contract"
        )

    def preference(row: tuple[Any, ...]) -> tuple[Any, ...]:
        order = routing.get("order", [])
        rank = next(
            (
                i
                for i, value in enumerate(order)
                if row[1].lower() == value.lower() or row[1].lower().startswith(value.lower() + "/")
            ),
            len(order),
        )
        return rank, row[0], row[1]

    units, tag, ceiling, tool_contract = min(choices, key=preference)
    extra["provider"] = {
        **routing,
        "only": [tag],
        "order": [tag],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {k: float(v) for k, v in ceiling.items()},
    }
    return units, {
        **kwargs,
        **tool_contract,
        "extra_body": extra,
        "max_tokens": requested,
        "num_retries": 0,
        "api_base": "https://openrouter.ai/api/v1",
    }


class OpenRouterQuotes:
    """Anonymous, short-lived endpoint metadata; no credentials or registry guesses."""

    def __init__(self) -> None:
        self.cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def __call__(self, kwargs: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        model = kwargs.get("model", "")
        if not re.fullmatch(r"openrouter/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+", model):
            raise RequestBudgetError("No bounded pricing policy for this model")
        cached = self.cache.get(model)
        if cached is None or time.monotonic() - cached[0] > 60:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.get(
                        "https://openrouter.ai/api/v1/models/"
                        + model.removeprefix("openrouter/")
                        + "/endpoints"
                    )
                    response.raise_for_status()
                    data = response.json()["data"]
                    if data["id"] != model.removeprefix("openrouter/") or not isinstance(
                        data["endpoints"], list
                    ):
                        raise ValueError("Unexpected model metadata")
                    cached = (time.monotonic(), data["endpoints"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                raise RequestBudgetError("Current provider pricing could not be verified") from None
            self.cache[model] = cached
        return openrouter_quote(kwargs, cached[1])
