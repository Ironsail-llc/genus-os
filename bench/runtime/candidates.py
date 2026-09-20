"""Bounded, fixture-only candidates using public framework APIs.

These are screening adapters, not production-selectable runtimes. The host owns
identity, action admission, verification and durable business state. No framework
filesystem, shell or delegation tool may escape the supplied gateway.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field


@dataclass
class FixtureGateway:
    tenant: str
    # Explicit synthetic state: no business credentials or network handlers.
    values: dict[str, str] = field(default_factory=dict)
    stopped: bool = False
    writes: int = 0
    dispatches: int = 0

    @property
    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "record",
                    "description": "Store an authorized value",
                    "parameters": SCHEMA,
                },
            }
        ]

    @property
    def verified(self):
        return self.values.get("report") == "delivered" and self.writes == 1

    async def invoke(self, tenant, name, arguments):
        self.admit(tenant)
        if name != "record":
            raise ValueError("framework tool bypass denied")
        return await self.dispatch(tenant, **arguments)

    def admit(self, tenant: str) -> None:
        if tenant != self.tenant or self.stopped:
            raise ValueError("tenant authority denied or stopped")

    async def dispatch(
        self, tenant: str, key: str | None = None, value: str | None = None, **extra
    ) -> dict:
        self.admit(tenant)
        self.dispatches += 1
        if extra or (key, value) != ("report", "delivered"):
            return {"error": "Only storing key report with value delivered is authorized."}
        if key in self.values and self.values[key] != value:
            raise ValueError("conflicting operation; reconcile instead of retrying")
        if key not in self.values:
            self.values[key] = value
            self.writes += 1
        return {"verification": "verified", "key": key, "value": self.values[key]}


SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
    "required": ["key", "value"],
    "additionalProperties": False,
}
PROMPT = (
    "Store key report with value delivered using record. Stop after independently verified success."
)
SYSTEM = "Complete only the authorized request. Use record to store the requested value."


def candidate_timeout():
    from robothor.engine.runtime.current import active_context
    from robothor.engine.runtime.deadlines import owns_deadline

    context = active_context.get()
    return asyncio.timeout(None if context and owns_deadline(context) else 60)


async def admit_gateway(gateway, tenant):
    from robothor.engine.runtime.deadlines import require_time

    require_time()
    result = gateway.admit(tenant)
    if inspect.isawaitable(result):
        await result


def bound_tools(gateway, tenant):
    """Bind host-selected names in closures, never in model-overridable arguments."""

    def bind(name):
        async def invoke(**arguments):
            return await gateway.invoke(tenant, name, arguments)

        return invoke

    for schema in gateway.schemas:
        definition = schema["function"]
        yield definition, bind(definition["name"])


class PydanticCandidate:
    def __init__(self, model, *, system_prompt=SYSTEM, request_budget=None, model_settings=None):
        if request_budget is not None:
            from bench.runtime.budgeted_models import BudgetedPydanticModel

            model = BudgetedPydanticModel(model, request_budget)
        self.model = model
        self.system_prompt = system_prompt
        self.model_settings = {"max_tokens": 512, "temperature": 0.5, **(model_settings or {})}

    async def run(self, gateway, *, tenant, prompt=PROMPT):
        await admit_gateway(gateway, tenant)
        from pydantic_ai import Agent, Tool
        from pydantic_ai.usage import UsageLimits

        agent = Agent(
            self.model,
            system_prompt=self.system_prompt,
            retries=0,
            model_settings=self.model_settings,
            tools=[
                Tool.from_schema(
                    invoke, spec["name"], spec.get("description", ""), spec["parameters"]
                )
                for spec, invoke in bound_tools(gateway, tenant)
            ],
        )
        started = time.perf_counter()
        async with (
            candidate_timeout(),
            agent.iter(
                prompt, usage_limits=UsageLimits(request_limit=4, tool_calls_limit=4)
            ) as run,
        ):
            async for _node in run:
                await admit_gateway(gateway, tenant)
                if gateway.verified:
                    break
            usage = run.usage
        return {
            "duration_ms": (time.perf_counter() - started) * 1000,
            "model_calls": usage.requests,
            # SDK zero defaults do not establish provider-reported zero usage.
            "input_tokens": usage.input_tokens or None,
            "output_tokens": usage.output_tokens or None,
            "usage_source": "framework",
            "cost_usd": None,
            "verified": gateway.verified,
        }


class DeepAgentsCandidate:
    def __init__(
        self,
        model,
        *,
        system_prompt=SYSTEM,
        request_budget=None,
        model_settings=None,
        tool_choice=None,
    ):
        if request_budget is not None:
            from bench.runtime.budgeted_models import BudgetedDeepModel

            model = BudgetedDeepModel(
                wrapped=model, budget=request_budget, profile=getattr(model, "profile", None)
            )
        self.model = model
        self.system_prompt = system_prompt
        self.model_settings = dict(model_settings or {})
        self.tool_choice = tool_choice

    async def run(self, gateway, *, tenant, prompt=PROMPT):
        await admit_gateway(gateway, tenant)
        from deepagents import create_deep_agent
        from langchain.agents.middleware import AgentMiddleware
        from langchain.agents.middleware.types import ModelResponse
        from langchain_core.messages import AIMessage, SystemMessage
        from langchain_core.tools import StructuredTool

        host_tools = {
            spec["name"]: StructuredTool.from_function(
                coroutine=invoke,
                name=spec["name"],
                description=spec.get("description", ""),
                args_schema=spec["parameters"],
            )
            for spec, invoke in bound_tools(gateway, tenant)
        }
        system_prompt = self.system_prompt
        model_settings, tool_choice = self.model_settings, self.tool_choice
        calls = 0
        tokens = {"input_tokens": 0, "output_tokens": 0}
        usage_known = True

        class Boundary(AgentMiddleware):
            async def awrap_model_call(self, request, handler):
                nonlocal calls, usage_known
                await admit_gateway(gateway, tenant)
                if gateway.verified:
                    return ModelResponse(result=[AIMessage(content="Verified completion")])
                if calls >= 4:
                    raise ValueError("model call bound exceeded")
                calls += 1
                response = await handler(
                    request.override(
                        tools=list(host_tools.values()),
                        model_settings={**request.model_settings, **model_settings},
                        tool_choice=tool_choice if tool_choice is not None else request.tool_choice,
                        system_message=SystemMessage(content=system_prompt),
                    )
                )
                usage_known &= bool(response.result)
                for message in response.result:
                    usage = getattr(message, "usage_metadata", None)
                    if not usage or any(key not in usage for key in tokens):
                        usage_known = False
                    else:
                        for key in tokens:
                            tokens[key] += usage[key]
                return response

            async def awrap_tool_call(self, request, handler):
                await admit_gateway(gateway, tenant)
                tool = host_tools.get(request.tool_call["name"])
                if tool is None:
                    raise ValueError("framework tool bypass denied")
                return await handler(request.override(tool=tool))

        started = time.perf_counter()
        agent = create_deep_agent(
            self.model,
            tools=list(host_tools.values()),
            system_prompt=system_prompt,
            middleware=[Boundary()],
        )
        async with candidate_timeout():
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": 12}
            )
        return {
            "duration_ms": (time.perf_counter() - started) * 1000,
            "model_calls": calls,
            **{key: value if usage_known else None for key, value in tokens.items()},
            "usage_source": "framework" if usage_known else None,
            "cost_usd": None,
            "verified": gateway.verified,
        }
