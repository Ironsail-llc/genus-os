"""Tests for hook system enhancements — new events, chaining, timeout, metrics."""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.hook_registry import (
    HookAction,
    HookContext,
    HookEvent,
    HookMetrics,
    HookRegistry,
    HookResult,
    LifecycleHook,
)

# ─── New Hook Events ──────────────────────────────────────────────────


class TestNewHookEvents:
    def test_new_hook_events_exist(self):
        """All 6 new lifecycle events are present in the enum."""
        expected = {
            "PRE_COMPACTION",
            "POST_COMPACTION",
            "BUDGET_WARNING",
            "CHECKPOINT",
            "PLAN_CREATED",
            "REPLAN",
        }
        actual = {e.name for e in HookEvent}
        assert expected.issubset(actual), f"Missing events: {expected - actual}"


# ─── LifecycleHook Defaults ───────────────────────────────────────────


class TestLifecycleHookDefaults:
    def test_hook_chain_mode_default(self):
        """Default chain_mode is 'short_circuit'."""
        hook = LifecycleHook(event=HookEvent.AGENT_START, handler_type="python", handler="x")
        assert hook.chain_mode == "short_circuit"

    def test_hook_timeout_default(self):
        """Default timeout is 30 seconds."""
        hook = LifecycleHook(event=HookEvent.AGENT_START, handler_type="python", handler="x")
        assert hook.timeout == 30

    def test_lifecycle_hook_chain_mode_field(self):
        """LifecycleHook accepts and stores chain_mode."""
        hook = LifecycleHook(
            event=HookEvent.AGENT_START,
            handler_type="python",
            handler="x",
            chain_mode="chain",
        )
        assert hook.chain_mode == "chain"


# ─── Chain Mode Dispatch ─────────────────────────────────────────────


class TestChainMode:
    @pytest.mark.asyncio
    async def test_chain_mode_accumulates_block(self):
        """Two chain hooks — one MODIFY, one BLOCK. BLOCK wins."""
        reg = HookRegistry()

        def modifier(ctx):
            return HookResult(
                action=HookAction.MODIFY,
                modified_args={"key": "modified"},
            )

        def blocker(ctx):
            return HookResult(action=HookAction.BLOCK, reason="blocked")

        reg.register_python_handler("modifier", modifier)
        reg.register_python_handler("blocker", blocker)

        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="modifier",
                blocking=True,
                chain_mode="chain",
                priority=10,
            )
        )
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="blocker",
                blocking=True,
                chain_mode="chain",
                priority=20,
            )
        )

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_args={"key": "original"})
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.BLOCK
        assert result.reason == "blocked"

    @pytest.mark.asyncio
    async def test_chain_mode_accumulates_modify(self):
        """Two chain hooks both MODIFY different args — all modifications applied."""
        reg = HookRegistry()

        def mod_a(ctx):
            args = dict(ctx.tool_args)
            args["a"] = "set_by_a"
            return HookResult(action=HookAction.MODIFY, modified_args=args)

        def mod_b(ctx):
            args = dict(ctx.tool_args)
            args["b"] = "set_by_b"
            return HookResult(action=HookAction.MODIFY, modified_args=args)

        reg.register_python_handler("mod_a", mod_a)
        reg.register_python_handler("mod_b", mod_b)

        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="mod_a",
                blocking=True,
                chain_mode="chain",
                priority=10,
            )
        )
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="mod_b",
                blocking=True,
                chain_mode="chain",
                priority=20,
            )
        )

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_args={})
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.MODIFY
        # mod_a runs first, sets a; mod_b sees updated tool_args (with a), sets b
        assert result.modified_args["a"] == "set_by_a"
        assert result.modified_args["b"] == "set_by_b"

    @pytest.mark.asyncio
    async def test_short_circuit_returns_first_block(self):
        """Two short_circuit blocking hooks — second never runs after first BLOCKs."""
        reg = HookRegistry()
        call_order = []

        def first_blocker(ctx):
            call_order.append("first")
            return HookResult(action=HookAction.BLOCK, reason="first blocked")

        def second_blocker(ctx):
            call_order.append("second")
            return HookResult(action=HookAction.BLOCK, reason="second blocked")

        reg.register_python_handler("first", first_blocker)
        reg.register_python_handler("second", second_blocker)

        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="first",
                blocking=True,
                chain_mode="short_circuit",
                priority=10,
            )
        )
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="second",
                blocking=True,
                chain_mode="short_circuit",
                priority=20,
            )
        )

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE)
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.BLOCK
        assert result.reason == "first blocked"
        assert call_order == ["first"]


# ─── Timeout ─────────────────────────────────────────────────────────


class TestHookTimeout:
    @pytest.mark.asyncio
    async def test_hook_timeout_returns_allow(self):
        """A python hook that exceeds its timeout returns ALLOW (fail-open)."""
        reg = HookRegistry()

        async def slow_handler(ctx):
            await asyncio.sleep(5)
            return HookResult(action=HookAction.BLOCK, reason="should not reach")

        reg.register_python_handler("slow", slow_handler)

        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="python",
            handler="slow",
            blocking=True,
            timeout=0.1,  # 100ms
        )
        reg.register(hook)

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE)
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.ALLOW

        # Verify metrics.timeouts was incremented
        metrics = reg.get_metrics()
        key = ("slow", HookEvent.PRE_TOOL_USE.value)
        assert key in metrics
        assert metrics[key].timeouts >= 1


# ─── Metrics ─────────────────────────────────────────────────────────


class TestHookMetrics:
    def test_hook_metrics_dataclass(self):
        """HookMetrics has all expected fields with correct defaults."""
        m = HookMetrics()
        assert m.executions == 0
        assert m.failures == 0
        assert m.total_duration_ms == 0.0
        assert m.timeouts == 0
        assert m.last_executed == 0.0

    @pytest.mark.asyncio
    async def test_hook_metrics_tracking(self):
        """Dispatching a hook updates executions, duration, and last_executed."""
        reg = HookRegistry()

        def handler(ctx):
            return HookResult(action=HookAction.ALLOW)

        reg.register_python_handler("tracked", handler)
        reg.register(
            LifecycleHook(
                event=HookEvent.AGENT_START,
                handler_type="python",
                handler="tracked",
                blocking=True,
            )
        )

        ctx = HookContext(event=HookEvent.AGENT_START)
        await reg.dispatch(HookEvent.AGENT_START, ctx)

        metrics = reg.get_metrics()
        key = ("tracked", HookEvent.AGENT_START.value)
        assert key in metrics
        m = metrics[key]
        assert m.executions == 1
        assert m.total_duration_ms > 0
        assert m.last_executed > 0

    @pytest.mark.asyncio
    async def test_hook_metrics_failure(self):
        """A hook that raises increments metrics.failures."""
        reg = HookRegistry()

        def broken(ctx):
            raise RuntimeError("boom")

        reg.register_python_handler("broken", broken)
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="broken",
                blocking=True,
            )
        )

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE)
        # Dispatch should not raise (fail-open), but metrics should record failure
        await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)

        metrics = reg.get_metrics()
        key = ("broken", HookEvent.PRE_TOOL_USE.value)
        assert key in metrics
        assert metrics[key].failures >= 1
        assert metrics[key].executions >= 1

    def test_get_metrics_returns_copy(self):
        """get_metrics() returns a dict (copy), not the internal reference."""
        reg = HookRegistry()
        m1 = reg.get_metrics()
        m2 = reg.get_metrics()
        assert isinstance(m1, dict)
        assert isinstance(m2, dict)
        assert m1 is not m2  # different dict objects
