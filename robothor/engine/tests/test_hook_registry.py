"""Tests for the lifecycle hook registry."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from robothor.engine.hook_registry import (
    HookAction,
    HookContext,
    HookEvent,
    HookRegistry,
    HookResult,
    LifecycleHook,
    get_hook_registry,
    init_hook_registry,
    load_global_hooks,
    load_hooks_from_manifest,
)

# ─── Registration Tests ─────────────────────────────────────────────


class TestHookRegistration:
    def test_register_and_count(self):
        reg = HookRegistry()
        assert reg.hook_count == 0
        reg.register(LifecycleHook(event=HookEvent.AGENT_START, handler_type="python", handler="x"))
        assert reg.hook_count == 1

    def test_register_many(self):
        reg = HookRegistry()
        hooks = [
            LifecycleHook(event=HookEvent.AGENT_START, handler_type="python", handler="a"),
            LifecycleHook(event=HookEvent.AGENT_END, handler_type="python", handler="b"),
        ]
        reg.register_many(hooks)
        assert reg.hook_count == 2

    def test_clear(self):
        reg = HookRegistry()
        reg.register(LifecycleHook(event=HookEvent.AGENT_START, handler_type="python", handler="x"))
        reg.clear()
        assert reg.hook_count == 0

    def test_priority_ordering(self):
        reg = HookRegistry()
        reg.register(
            LifecycleHook(
                event=HookEvent.AGENT_START, handler_type="python", handler="low", priority=200
            )
        )
        reg.register(
            LifecycleHook(
                event=HookEvent.AGENT_START, handler_type="python", handler="high", priority=10
            )
        )
        hooks = reg.get_hooks_for_event(HookEvent.AGENT_START)
        assert hooks[0].handler == "high"
        assert hooks[1].handler == "low"


# ─── Event Filtering Tests ──────────────────────────────────────────


class TestEventFiltering:
    def test_filters_by_event_type(self):
        reg = HookRegistry()
        reg.register(LifecycleHook(event=HookEvent.AGENT_START, handler_type="python", handler="a"))
        reg.register(LifecycleHook(event=HookEvent.AGENT_END, handler_type="python", handler="b"))
        assert len(reg.get_hooks_for_event(HookEvent.AGENT_START)) == 1
        assert len(reg.get_hooks_for_event(HookEvent.ERROR)) == 0

    def test_scope_filtering_global_matches_all(self):
        reg = HookRegistry()
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="g",
                scope="global",
                agent_id="",
            )
        )
        hooks = reg.get_hooks_for_event(HookEvent.PRE_TOOL_USE, agent_id="any-agent")
        assert len(hooks) == 1

    def test_scope_filtering_agent_matches_own(self):
        reg = HookRegistry()
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="a",
                scope="agent",
                agent_id="email-classifier",
            )
        )
        assert (
            len(reg.get_hooks_for_event(HookEvent.PRE_TOOL_USE, agent_id="email-classifier")) == 1
        )
        assert len(reg.get_hooks_for_event(HookEvent.PRE_TOOL_USE, agent_id="main")) == 0

    def test_tool_name_filter_glob(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="python",
            handler="x",
            filter={"tool_name": "exec*"},
        )
        ctx_match = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="exec_command")
        ctx_no_match = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="read_file")
        assert reg._matches_filter(hook, ctx_match) is True
        assert reg._matches_filter(hook, ctx_no_match) is False

    def test_empty_filter_matches_all(self):
        reg = HookRegistry()
        hook = LifecycleHook(event=HookEvent.PRE_TOOL_USE, handler_type="python", handler="x")
        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="anything")
        assert reg._matches_filter(hook, ctx) is True


# ─── Dispatch Tests ──────────────────────────────────────────────────


class TestDispatch:
    @pytest.mark.asyncio
    async def test_no_hooks_returns_allow(self):
        reg = HookRegistry()
        ctx = HookContext(event=HookEvent.AGENT_START, agent_id="main")
        result = await reg.dispatch(HookEvent.AGENT_START, ctx)
        assert result.action == HookAction.ALLOW

    @pytest.mark.asyncio
    async def test_blocking_allow(self):
        reg = HookRegistry()
        called = []

        def handler(ctx):
            called.append(True)
            return HookResult(action=HookAction.ALLOW)

        reg.register_python_handler("my_handler", handler)
        reg.register(
            LifecycleHook(
                event=HookEvent.AGENT_START,
                handler_type="python",
                handler="my_handler",
                blocking=True,
            )
        )
        ctx = HookContext(event=HookEvent.AGENT_START)
        result = await reg.dispatch(HookEvent.AGENT_START, ctx)
        assert result.action == HookAction.ALLOW
        assert called == [True]

    @pytest.mark.asyncio
    async def test_blocking_block_short_circuits(self):
        reg = HookRegistry()
        call_order = []

        def blocker(ctx):
            call_order.append("blocker")
            return HookResult(action=HookAction.BLOCK, reason="nope")

        def after(ctx):
            call_order.append("after")
            return HookResult()

        reg.register_python_handler("blocker", blocker)
        reg.register_python_handler("after", after)
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="blocker",
                blocking=True,
                priority=10,
            )
        )
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="after",
                blocking=True,
                priority=20,
            )
        )

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE)
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.BLOCK
        assert result.reason == "nope"
        assert call_order == ["blocker"]  # "after" never called

    @pytest.mark.asyncio
    async def test_blocking_modify_returns_modified_args(self):
        reg = HookRegistry()

        def modifier(ctx):
            return HookResult(
                action=HookAction.MODIFY,
                modified_args={"path": "/safe/path"},
            )

        reg.register_python_handler("mod", modifier)
        reg.register(
            LifecycleHook(
                event=HookEvent.PRE_TOOL_USE,
                handler_type="python",
                handler="mod",
                blocking=True,
            )
        )
        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_args={"path": "/dangerous"})
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.MODIFY
        assert result.modified_args == {"path": "/safe/path"}

    @pytest.mark.asyncio
    async def test_non_blocking_fires_and_forgets(self):
        reg = HookRegistry()
        called = []

        async def observer(ctx):
            called.append(ctx.agent_id)

        reg.register_python_handler("obs", observer)
        reg.register(
            LifecycleHook(
                event=HookEvent.AGENT_END,
                handler_type="python",
                handler="obs",
                blocking=False,
            )
        )
        ctx = HookContext(event=HookEvent.AGENT_END, agent_id="test")
        result = await reg.dispatch(HookEvent.AGENT_END, ctx)
        assert result.action == HookAction.ALLOW
        # Give the fire-and-forget task time to run
        await asyncio.sleep(0.05)
        assert called == ["test"]

    @pytest.mark.asyncio
    async def test_non_blocking_error_doesnt_crash(self):
        reg = HookRegistry()

        def broken(ctx):
            raise RuntimeError("boom")

        reg.register_python_handler("broken", broken)
        reg.register(
            LifecycleHook(
                event=HookEvent.AGENT_END,
                handler_type="python",
                handler="broken",
                blocking=False,
            )
        )
        ctx = HookContext(event=HookEvent.AGENT_END)
        result = await reg.dispatch(HookEvent.AGENT_END, ctx)
        assert result.action == HookAction.ALLOW
        await asyncio.sleep(0.05)  # let error task complete

    @pytest.mark.asyncio
    async def test_blocking_error_fails_open(self):
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
        result = await reg.dispatch(HookEvent.PRE_TOOL_USE, ctx)
        assert result.action == HookAction.ALLOW  # fail-open


# ─── Python Handler Tests ────────────────────────────────────────────


class TestPythonHandler:
    @pytest.mark.asyncio
    async def test_sync_handler(self):
        reg = HookRegistry()

        def my_sync(ctx):
            return HookResult(action=HookAction.BLOCK, reason="sync")

        reg.register_python_handler("s", my_sync)
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE, handler_type="python", handler="s", blocking=True
        )
        result = await reg._run_python(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.BLOCK

    @pytest.mark.asyncio
    async def test_async_handler(self):
        reg = HookRegistry()

        async def my_async(ctx):
            return HookResult(action=HookAction.ALLOW, system_message="hi")

        reg.register_python_handler("a", my_async)
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE, handler_type="python", handler="a", blocking=True
        )
        result = await reg._run_python(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.ALLOW
        assert result.system_message == "hi"

    @pytest.mark.asyncio
    async def test_missing_handler_returns_allow(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="python",
            handler="nonexistent.module.func",
            blocking=True,
        )
        result = await reg._run_python(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.ALLOW


# ─── Command Handler Tests ───────────────────────────────────────────


class TestCommandHandler:
    @pytest.mark.asyncio
    async def test_exit_0_allows(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="command",
            handler="true",
            blocking=True,
        )
        result = await reg._run_command(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.ALLOW

    @pytest.mark.asyncio
    async def test_exit_1_blocks(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="command",
            handler="echo 'denied' && exit 1",
            blocking=True,
        )
        result = await reg._run_command(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.BLOCK
        assert "denied" in result.reason

    @pytest.mark.asyncio
    async def test_json_stdout_modify(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="command",
            handler="""echo '{"action": "modify", "args": {"safe": true}}'""",
            blocking=True,
        )
        result = await reg._run_command(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.MODIFY
        assert result.modified_args == {"safe": True}

    @pytest.mark.asyncio
    async def test_timeout_returns_allow(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="command",
            handler="sleep 30",
            blocking=True,
        )
        # The handler has a 10s timeout, but we mock subprocess for speed
        with patch("robothor.engine.hook_registry.subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired("sleep 30", 10)
            result = await reg._run_command(hook, HookContext(event=HookEvent.PRE_TOOL_USE))
        assert result.action == HookAction.ALLOW

    @pytest.mark.asyncio
    async def test_env_vars_passed(self):
        reg = HookRegistry()
        hook = LifecycleHook(
            event=HookEvent.PRE_TOOL_USE,
            handler_type="command",
            handler="echo $HOOK_TOOL_NAME",
            blocking=True,
        )
        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE, tool_name="exec", agent_id="main", run_id="r1"
        )
        result = await reg._run_command(hook, ctx)
        assert result.action == HookAction.ALLOW


# ─── Manifest Loading Tests ──────────────────────────────────────────


class TestManifestLoading:
    def test_parse_lifecycle_hooks(self):
        manifest = {
            "v2": {
                "lifecycle_hooks": [
                    {
                        "event": "pre_tool_use",
                        "handler_type": "python",
                        "handler": "my.module.check",
                        "blocking": True,
                        "priority": 50,
                        "filter": {"tool_name": "exec*"},
                    },
                    {
                        "event": "agent_end",
                        "handler_type": "command",
                        "handler": "echo done",
                    },
                ]
            }
        }
        hooks = load_hooks_from_manifest(manifest, "test-agent")
        assert len(hooks) == 2
        assert hooks[0].event == HookEvent.PRE_TOOL_USE
        assert hooks[0].blocking is True
        assert hooks[0].priority == 50
        assert hooks[0].agent_id == "test-agent"
        assert hooks[1].event == HookEvent.AGENT_END
        assert hooks[1].handler == "echo done"

    def test_skip_invalid_events(self):
        manifest = {
            "v2": {
                "lifecycle_hooks": [
                    {"event": "not_real", "handler_type": "python", "handler": "x"},
                ]
            }
        }
        hooks = load_hooks_from_manifest(manifest, "test")
        assert len(hooks) == 0

    def test_empty_list(self):
        hooks = load_hooks_from_manifest({"v2": {}}, "test")
        assert hooks == []

    def test_no_v2_block(self):
        hooks = load_hooks_from_manifest({}, "test")
        assert hooks == []


class TestGlobalHooksLoading:
    def test_missing_file_returns_empty(self, tmp_path):
        hooks = load_global_hooks(tmp_path)
        assert hooks == []

    def test_loads_from_yaml(self, tmp_path):
        import yaml

        global_file = tmp_path / "global.yaml"
        global_file.write_text(
            yaml.dump(
                {
                    "hooks": [
                        {
                            "event": "agent_start",
                            "handler_type": "command",
                            "handler": "echo hi",
                            "blocking": False,
                        }
                    ]
                }
            )
        )
        hooks = load_global_hooks(tmp_path)
        assert len(hooks) == 1
        assert hooks[0].scope == "global"
        assert hooks[0].agent_id == ""

    def test_empty_hooks_file(self, tmp_path):
        (tmp_path / "global.yaml").write_text("hooks: []\n")
        hooks = load_global_hooks(tmp_path)
        assert hooks == []


# ─── Singleton Tests ─────────────────────────────────────────────────


class TestSingleton:
    def test_init_and_get(self):
        reg = init_hook_registry()
        assert get_hook_registry() is reg

    def test_get_before_init(self):
        import robothor.engine.hook_registry as mod

        old = mod._hook_registry
        try:
            mod._hook_registry = None
            assert get_hook_registry() is None
        finally:
            mod._hook_registry = old


# ─── HookContext Tests ───────────────────────────────────────────────


class TestHookContext:
    def test_fields_populated(self):
        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            agent_id="main",
            run_id="r1",
            tool_name="exec",
            tool_args={"cmd": "ls"},
        )
        assert ctx.event == HookEvent.PRE_TOOL_USE
        assert ctx.agent_id == "main"
        assert ctx.tool_name == "exec"
        assert ctx.tool_args == {"cmd": "ls"}

    def test_default_values(self):
        ctx = HookContext(event=HookEvent.AGENT_START)
        assert ctx.agent_id == ""
        assert ctx.tool_name == ""
        assert ctx.tool_args == {}
        assert ctx.tool_result is None
