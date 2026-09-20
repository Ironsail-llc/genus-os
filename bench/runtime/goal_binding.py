"""Bind experimental execution to the existing trusted goal-controller scope."""

import asyncio
from copy import copy, deepcopy

from bench.runtime.budgeted_models import BudgetedDeepModel, BudgetedPydanticModel, RequestBudget
from bench.runtime.candidates import PydanticCandidate
from robothor.goals.provider_ledger import DurableAttemptBudget
from robothor.goals.runtime import binding


def trusted_goal(context):
    current = binding.get()
    if not context.goal_id:
        if current:
            raise ValueError("goal-bound work cannot omit its trusted goal identity")
        return None
    if not current or (current.tenant, current.goal_id, current.attempt) != (
        context.tenant_id,
        context.goal_id,
        context.attempt_id,
    ):
        raise ValueError("candidate goal requires the active trusted lease binding")
    return current


async def bind_goal(context, maximum):
    current = trusted_goal(context)
    if current is None:
        return None
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("candidate goal requires a trusted positive provider token bound")
    ledger = DurableAttemptBudget(context.tenant_id, context.goal_id, context.attempt_id)
    await asyncio.to_thread(ledger.validate_context, context)
    # The existing controller and delegated native calls see this same durable ledger.
    current.provider_budget = ledger
    return ledger


def bind_candidate(context, candidate, maximum):
    current = trusted_goal(context)
    if current is None:
        return candidate
    if isinstance(candidate.model, BudgetedPydanticModel | BudgetedDeepModel):
        raise ValueError("candidate already has a budget owner")
    ledger = current.provider_budget
    if not isinstance(ledger, DurableAttemptBudget):
        raise ValueError("durable goal budget was not admitted")
    budget = RequestBudget(ledger, maximum)
    bound = copy(candidate)
    bound.model_settings = deepcopy(candidate.model_settings)
    bound.model = (
        BudgetedPydanticModel(candidate.model, budget)
        if isinstance(candidate, PydanticCandidate)
        else BudgetedDeepModel(
            wrapped=candidate.model,
            budget=budget,
            profile=getattr(candidate.model, "profile", None),
        )
    )
    return bound
