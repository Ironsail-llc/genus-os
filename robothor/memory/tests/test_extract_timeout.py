"""Extraction must not be given a budget larger than its caller's wall.

`extract_facts` wrapped its work in `asyncio.wait_for(..., timeout=180.0)`
while `store_memory` runs inside a 120s tool timeout (runner.py:2471, enforced
at tools/registry.py). The inner budget therefore exceeded the outer one: a
slow extraction could never return, it was guaranteed to be killed first. Over
30 days that shows up as 15 of 121 store_memory calls sitting at exactly
120,003 ms — the wall, hit precisely — and a 13.2% failure rate.

The fix is to parameterise, not to lower the constant. Off-path callers
(ingestion, the eval) legitimately want the full 180s because nothing is
waiting on them; only the request path needs a shorter budget.

Measured attribution, with the model warm: extraction is 22.98s and embedding
is 0.12s, so this timeout governs essentially the whole call. Batching the
embeddings — the originally planned first fix — would have saved ~0.2s.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from robothor.memory import facts


def test_extract_facts_accepts_a_timeout():
    sig = inspect.signature(facts.extract_facts)
    assert "timeout" in sig.parameters, (
        "extract_facts must take a timeout so the request path can pass a budget "
        "smaller than the tool wall"
    )


def test_default_preserves_the_off_path_budget():
    """ingestion and the eval run off the request path and should keep 180s."""
    assert inspect.signature(facts.extract_facts).parameters["timeout"].default == 180.0


@pytest.mark.asyncio
async def test_timeout_is_honoured(monkeypatch):
    async def _never_returns(*_a, **_kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(facts, "_extract_facts_inner", _never_returns)

    started = asyncio.get_running_loop().time()
    result = await facts.extract_facts("anything", timeout=0.05)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 5, f"timeout not applied — waited {elapsed:.2f}s"
    assert result == [], "a timed-out extraction yields no facts"


@pytest.mark.asyncio
async def test_timeout_returns_empty_rather_than_raising(monkeypatch):
    """The caller stores the raw content when extraction yields nothing, so a
    timeout must degrade rather than propagate. Distinguishing the two states is
    tracked separately — a shorter budget makes the raw-fallback path fire more
    often, which increases churn."""

    async def _boom(*_a, **_kw):
        raise TimeoutError

    monkeypatch.setattr(facts, "_extract_facts_inner", _boom)
    assert await facts.extract_facts("anything", timeout=1.0) == []


def test_request_path_budget_is_under_the_tool_wall():
    """Pin the relationship that was inverted.

    The handler must pass a budget strictly below the 120s tool timeout,
    otherwise the fix is cosmetic: the tool would still be killed before
    extraction could return.
    """
    src = inspect.getsource(__import__("robothor.engine.tools.handlers.memory", fromlist=["x"]))
    assert "extract_facts(" in src
    assert "timeout=" in src.split("extract_facts(")[1][:200], (
        "store_memory calls extract_facts without a timeout, so it inherits the "
        "180s default inside a 120s wall"
    )
