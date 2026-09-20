"""Provider-boundary reservations for experimental adapters, using public APIs.

The host supplies a conservative per-request token bound and a shared ledger.
These wrappers do not infer a safe token bound or provide durable goal storage.
Unknown usage and interrupted calls keep their reservation until reconciled.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict
from pydantic_ai.models.wrapper import WrapperModel


class RequestBudget:
    def __init__(self, ledger, maximum_tokens):
        if type(maximum_tokens) is not int or maximum_tokens <= 0:
            raise ValueError("positive trusted request bound required")
        self.ledger = ledger
        self.maximum_tokens = maximum_tokens

    async def reserve(self):
        identity = str(uuid4())
        await asyncio.to_thread(self.ledger.reserve, identity, self.maximum_tokens)
        return identity

    async def settle(self, identity, input_tokens, output_tokens):
        # Zero-default SDK counters cannot establish that usage was reported.
        actual = None
        if (
            type(input_tokens) is int
            and type(output_tokens) is int
            and input_tokens > 0
            and output_tokens > 0
        ):
            actual = input_tokens + output_tokens
        await asyncio.to_thread(self.ledger.settle, identity, actual)


class BudgetedPydanticModel(WrapperModel):
    def __init__(self, wrapped, budget):
        super().__init__(wrapped)
        self.budget = budget

    async def request(self, messages, model_settings, model_request_parameters):
        identity = await self.budget.reserve()
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        await self.budget.settle(
            identity, response.usage.input_tokens, response.usage.output_tokens
        )
        return response

    def request_stream(self, *args, **kwargs):
        raise NotImplementedError("budgeted candidate streaming requires usage reconciliation")


class BudgetedDeepModel(BaseChatModel):
    """Keep the reservation boundary when tools are bound or helpers reuse the model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    wrapped: object
    budget: RequestBudget

    @property
    def _llm_type(self):
        return "robothor-budgeted-candidate"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"wrapped": self.wrapped.bind_tools(tools, **kwargs)})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("budgeted candidates require async execution")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        identity = await self.budget.reserve()
        response = await self.wrapped.ainvoke(messages, stop=stop, **kwargs)
        usage = response.usage_metadata or {}
        await self.budget.settle(identity, usage.get("input_tokens"), usage.get("output_tokens"))
        return ChatResult(generations=[ChatGeneration(message=response)])
