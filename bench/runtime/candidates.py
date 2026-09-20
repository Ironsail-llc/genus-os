"""Bounded, fixture-only candidates using public framework APIs.

These are screening adapters, not production-selectable runtimes. The host owns
identity, action admission, verification and durable business state. No framework
filesystem, shell or delegation tool may escape the supplied gateway.
"""

from __future__ import annotations

import asyncio
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

    async def dispatch(
        self, tenant: str, key: str | None = None, value: str | None = None, **extra
    ) -> dict:
        if tenant != self.tenant or self.stopped:
            raise ValueError("tenant authority denied or stopped")
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


class PydanticCandidate:
    def __init__(self, model):
        self.model = model

    async def run(self, gateway, *, tenant, prompt=PROMPT):
        from pydantic_ai import Agent, Tool
        from pydantic_ai.usage import UsageLimits

        async def record(**kwargs):
            return await gateway.dispatch(tenant, **kwargs)

        agent = Agent(
            self.model,
            system_prompt=SYSTEM,
            retries=0,
            model_settings={"max_tokens": 512, "temperature": 0.5},
            tools=[Tool.from_schema(record, "record", "Store an authorized value", SCHEMA)],
        )
        started = time.perf_counter()
        async with (
            asyncio.timeout(60),
            agent.iter(
                prompt, usage_limits=UsageLimits(request_limit=4, tool_calls_limit=4)
            ) as run,
        ):
            async for _node in run:
                if gateway.values.get("report") == "delivered":
                    break
            usage = run.usage
        return {
            "duration_ms": (time.perf_counter() - started) * 1000,
            "model_calls": usage.requests,
            "verified": gateway.values.get("report") == "delivered",
        }


class DeepAgentsCandidate:
    def __init__(self, model):
        self.model = model

    async def run(self, gateway, *, tenant, prompt=PROMPT):
        from deepagents import create_deep_agent
        from langchain.agents.middleware import AgentMiddleware
        from langchain.agents.middleware.types import ModelResponse
        from langchain_core.messages import AIMessage, SystemMessage
        from langchain_core.tools import StructuredTool

        async def record(**kwargs):
            return await gateway.dispatch(tenant, **kwargs)

        record_tool = StructuredTool.from_function(
            coroutine=record,
            name="record",
            description="Store an authorized value",
            args_schema=SCHEMA,
        )
        calls = 0

        class Boundary(AgentMiddleware):
            async def awrap_model_call(self, request, handler):
                nonlocal calls
                if gateway.stopped:
                    raise ValueError("stopped")
                if gateway.values.get("report") == "delivered":
                    return ModelResponse(result=[AIMessage(content="Verified completion")])
                if calls >= 4:
                    raise ValueError("model call bound exceeded")
                calls += 1
                return await handler(
                    request.override(
                        tools=[record_tool], system_message=SystemMessage(content=SYSTEM)
                    )
                )

            async def awrap_tool_call(self, request, handler):
                if request.tool_call["name"] != "record" or gateway.stopped:
                    raise ValueError("framework tool bypass denied")
                return await handler(request)

        started = time.perf_counter()
        agent = create_deep_agent(
            self.model, tools=[record_tool], system_prompt=SYSTEM, middleware=[Boundary()]
        )
        async with asyncio.timeout(60):
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": 12}
            )
        return {
            "duration_ms": (time.perf_counter() - started) * 1000,
            "model_calls": calls,
            "verified": gateway.values.get("report") == "delivered",
        }
