"""Restart/retry behavior for external writes, including an uncertain timeout."""

import asyncio

import pytest

from robothor.operations.effects import Effects, UnresolvedEffect
from robothor.operations.store import Conflict


@pytest.mark.asyncio
async def test_successful_effect_is_reused_after_restart(ops):
    calls = []

    async def create():
        calls.append(1)
        return {"id": "external-1"}

    await Effects(ops.tenant).perform("provider.create", "company-1", {"name": "Example"}, create)
    assert await Effects(ops.tenant).perform(
        "provider.create", "company-1", {"name": "Example"}, create
    ) == {"id": "external-1"}
    assert len(calls) == 1
    with pytest.raises(Conflict):
        await Effects(ops.tenant).perform(
            "provider.create", "company-1", {"name": "Changed"}, create
        )


@pytest.mark.asyncio
async def test_parallel_workers_cannot_duplicate_an_external_effect(ops):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create():
        entered.set()
        await release.wait()
        return {"id": "external-1"}

    first = asyncio.create_task(
        Effects(ops.tenant).perform("provider.create", "company-1", {}, create)
    )
    await entered.wait()
    try:
        with pytest.raises(UnresolvedEffect):
            await Effects(ops.tenant).perform("provider.create", "company-1", {}, create)
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_uncertain_write_requires_reconciliation_before_replay(ops):
    calls = []

    async def timeout():
        calls.append(1)
        raise TimeoutError("private request data")

    effects = Effects(ops.tenant)
    with pytest.raises(UnresolvedEffect):
        await effects.perform("provider.create", "company-1", {}, timeout)
    with pytest.raises(UnresolvedEffect):
        await effects.perform("provider.create", "company-1", {}, timeout)
    assert len(calls) == 1
    effects.reconcile(
        "provider.create",
        "company-1",
        {"id": "found-1"},
        "operator:test",
        "Matched provider record",
    )
    assert await effects.perform("provider.create", "company-1", {}, timeout) == {"id": "found-1"}
