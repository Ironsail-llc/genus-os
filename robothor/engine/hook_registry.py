"""Lifecycle hook registry and dispatcher.

Collects hooks from global config and per-agent manifests, then dispatches
them at lifecycle points in the runner execution loop.

Separate from hooks.py (Redis Stream event triggers) — this system handles
fine-grained lifecycle interception with blocking, filtering, and multiple
handler types.
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import yaml

logger = logging.getLogger(__name__)


# ─── Enums ───────────────────────────────────────────────────────────


class HookEvent(StrEnum):
    """Lifecycle events that can trigger hooks."""

    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_DELIVERY = "pre_delivery"
    POST_DELIVERY = "post_delivery"
    ERROR = "error"
    ESCALATION = "escalation"
    STREAM_EVENT = "stream_event"
    PRE_COMPACTION = "pre_compaction"
    POST_COMPACTION = "post_compaction"
    BUDGET_WARNING = "budget_warning"
    CHECKPOINT = "checkpoint"
    PLAN_CREATED = "plan_created"
    REPLAN = "replan"


class HookAction(StrEnum):
    """Result action from a hook handler."""

    ALLOW = "allow"
    BLOCK = "block"
    MODIFY = "modify"


# ─── Data classes ────────────────────────────────────────────────────


@dataclass
class LifecycleHook:
    """A single lifecycle hook definition."""

    event: HookEvent
    handler_type: str  # "command", "http", "agent", "python"
    handler: str  # shell cmd, URL, agent_id, or dotted.path
    blocking: bool = False
    priority: int = 100  # lower = runs first
    filter: dict[str, str] = field(default_factory=dict)
    scope: str = "agent"  # "global", "agent", "workflow"
    agent_id: str = ""  # which agent this belongs to ("" = global)
    chain_mode: str = "short_circuit"  # "short_circuit" or "chain"
    timeout: int = 30  # seconds; 0 = no timeout


@dataclass
class HookResult:
    """Result from dispatching hooks for an event."""

    action: HookAction = HookAction.ALLOW
    modified_args: dict[str, Any] | None = None
    reason: str = ""
    system_message: str = ""


@dataclass
class HookMetrics:
    """Execution metrics for a hook handler."""

    executions: int = 0
    failures: int = 0
    total_duration_ms: float = 0.0
    timeouts: int = 0
    last_executed: float = 0.0  # time.monotonic()


@dataclass
class HookContext:
    """Context passed to hook handlers."""

    event: HookEvent
    agent_id: str = ""
    run_id: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    output_text: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Registry ────────────────────────────────────────────────────────


class HookRegistry:
    """Collects and dispatches lifecycle hooks."""

    _sync_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hook-sync")

    def __init__(self) -> None:
        self._hooks: list[LifecycleHook] = []
        self._python_handlers: dict[str, Callable[..., Any]] = {}
        self._metrics: dict[tuple[str, str], HookMetrics] = {}  # (handler, event) -> metrics

    def register(self, hook: LifecycleHook) -> None:
        """Register a lifecycle hook."""
        self._hooks.append(hook)
        self._hooks.sort(key=lambda h: h.priority)

    def register_many(self, hooks: list[LifecycleHook]) -> None:
        """Register multiple hooks at once."""
        self._hooks.extend(hooks)
        self._hooks.sort(key=lambda h: h.priority)

    def clear(self) -> None:
        """Remove all registered hooks."""
        self._hooks.clear()

    def register_python_handler(self, name: str, handler: Callable[..., Any]) -> None:
        """Register a Python callable for use with handler_type='python'."""
        self._python_handlers[name] = handler

    @property
    def hook_count(self) -> int:
        return len(self._hooks)

    def get_hooks_for_event(
        self,
        event: HookEvent,
        agent_id: str = "",
    ) -> list[LifecycleHook]:
        """Get matching hooks for an event, filtered by scope and agent."""
        matching = []
        for hook in self._hooks:
            if hook.event != event:
                continue
            if hook.scope == "agent" and hook.agent_id and hook.agent_id != agent_id:
                continue
            matching.append(hook)
        return matching

    async def dispatch(
        self,
        event: HookEvent,
        context: HookContext,
    ) -> HookResult:
        """Dispatch hooks for an event.

        Blocking hooks with chain_mode="short_circuit" (default):
            first BLOCK or MODIFY wins.
        Blocking hooks with chain_mode="chain":
            MODIFY updates context.tool_args and continues.
            BLOCK accumulates but continues.
            After all chain hooks: BLOCK > MODIFY > ALLOW.
        Non-blocking hooks: fire-and-forget via asyncio.create_task.
        """
        hooks = self.get_hooks_for_event(event, agent_id=context.agent_id)
        if not hooks:
            return HookResult()

        result = HookResult()
        chain_blocked = False
        chain_block_reason = ""
        chain_modified = False

        for hook in hooks:
            if not self._matches_filter(hook, context):
                continue

            if hook.blocking:
                try:
                    hr = await self._execute_handler(hook, context)

                    if hook.chain_mode == "chain":
                        # Chain mode: accumulate results, continue
                        if hr.action == HookAction.BLOCK:
                            chain_blocked = True
                            if hr.reason:
                                chain_block_reason = hr.reason
                        elif hr.action == HookAction.MODIFY:
                            chain_modified = True
                            if hr.modified_args:
                                context.tool_args = hr.modified_args
                                result.modified_args = hr.modified_args
                            if hr.system_message:
                                result.system_message = hr.system_message
                    else:
                        # Short-circuit mode (default): first BLOCK or MODIFY wins
                        if hr.action == HookAction.BLOCK:
                            return hr
                        if hr.action == HookAction.MODIFY:
                            if hr.modified_args:
                                context.tool_args = hr.modified_args
                            return hr
                except Exception as e:
                    logger.error("Blocking hook %s failed: %s", hook.handler, e)
                    # Fail-open: blocking hook error = allow
            else:
                asyncio.create_task(self._execute_handler_safe(hook, context))

        # Resolve accumulated chain results: BLOCK > MODIFY > ALLOW
        if chain_blocked:
            return HookResult(action=HookAction.BLOCK, reason=chain_block_reason)
        if chain_modified:
            result.action = HookAction.MODIFY
            return result

        return result

    def _matches_filter(self, hook: LifecycleHook, context: HookContext) -> bool:
        """Check if a hook's filter matches the current context."""
        if not hook.filter:
            return True
        for key, pattern in hook.filter.items():
            if key == "tool_name" and context.tool_name:
                if not fnmatch.fnmatch(context.tool_name, pattern):
                    return False
            elif (
                key == "agent_id"
                and context.agent_id
                and not fnmatch.fnmatch(context.agent_id, pattern)
            ):
                return False
        return True

    def _get_metrics(self, hook: LifecycleHook) -> HookMetrics:
        """Get or create metrics for a hook handler."""
        key = (hook.handler, hook.event.value)
        if key not in self._metrics:
            self._metrics[key] = HookMetrics()
        return self._metrics[key]

    def get_metrics(self) -> dict[tuple[str, str], HookMetrics]:
        """Return all hook metrics keyed by (handler, event)."""
        return dict(self._metrics)

    async def _execute_handler(self, hook: LifecycleHook, context: HookContext) -> HookResult:
        """Execute a single hook handler and return its result.

        Wraps execution with timeout (if configured) and metrics tracking.
        """
        metrics = self._get_metrics(hook)
        start = time.monotonic()

        try:
            coro = self._dispatch_handler(hook, context)
            if hook.timeout > 0:
                result = await asyncio.wait_for(coro, timeout=hook.timeout)
            else:
                result = await coro
        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            metrics.timeouts += 1
            metrics.executions += 1
            metrics.total_duration_ms += elapsed_ms
            metrics.last_executed = start
            logger.warning(
                "Hook %s timed out after %ds (fail-open)",
                hook.handler,
                hook.timeout,
            )
            return HookResult()
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            metrics.failures += 1
            metrics.executions += 1
            metrics.total_duration_ms += elapsed_ms
            metrics.last_executed = start
            raise
        else:
            elapsed_ms = (time.monotonic() - start) * 1000
            metrics.executions += 1
            metrics.total_duration_ms += elapsed_ms
            metrics.last_executed = start
            return result

    async def _dispatch_handler(self, hook: LifecycleHook, context: HookContext) -> HookResult:
        """Route to the correct handler implementation."""
        if hook.handler_type == "python":
            return await self._run_python(hook, context)
        elif hook.handler_type == "command":
            return await self._run_command(hook, context)
        elif hook.handler_type == "http":
            return await self._run_http(hook, context)
        elif hook.handler_type == "agent":
            return await self._run_agent(hook, context)
        else:
            logger.warning("Unknown handler type: %s", hook.handler_type)
            return HookResult()

    async def _execute_handler_safe(self, hook: LifecycleHook, context: HookContext) -> None:
        """Execute a handler, catching and logging any exception."""
        try:
            await self._execute_handler(hook, context)
        except Exception as e:
            logger.error("Non-blocking hook %s failed: %s", hook.handler, e)

    # ── Handler implementations ──────────────────────────────────────

    async def _run_python(self, hook: LifecycleHook, context: HookContext) -> HookResult:
        """Execute a registered Python callable or import by dotted path."""
        handler = self._python_handlers.get(hook.handler)
        if handler is None:
            try:
                module_path, func_name = hook.handler.rsplit(".", 1)
                module = importlib.import_module(module_path)
                handler = getattr(module, func_name)
            except Exception as e:
                logger.error("Failed to import handler %s: %s", hook.handler, e)
                return HookResult()

        if asyncio.iscoroutinefunction(handler):
            return await handler(context)
        else:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._sync_executor, handler, context)

    async def _run_command(self, hook: LifecycleHook, context: HookContext) -> HookResult:
        """Run shell command. Exit 0 = allow, 1 = block."""
        env = {
            **os.environ,
            "HOOK_EVENT": context.event.value,
            "HOOK_AGENT_ID": context.agent_id,
            "HOOK_RUN_ID": context.run_id,
            "HOOK_TOOL_NAME": context.tool_name,
        }

        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    hook.handler,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                ),
            )

            if proc.returncode == 0:
                result = HookResult(action=HookAction.ALLOW)
                if proc.stdout.strip():
                    try:
                        data = json.loads(proc.stdout)
                        if data.get("action") == "modify":
                            result.action = HookAction.MODIFY
                            result.modified_args = data.get("args")
                        if data.get("system_message"):
                            result.system_message = data["system_message"]
                    except json.JSONDecodeError:
                        pass
                return result
            elif proc.returncode == 1:
                reason = proc.stdout.strip() or proc.stderr.strip() or "Blocked by hook"
                return HookResult(action=HookAction.BLOCK, reason=reason)
            else:
                logger.warning("Hook command exited %d: %s", proc.returncode, hook.handler)
                return HookResult()

        except subprocess.TimeoutExpired:
            logger.warning("Hook command timed out: %s", hook.handler)
            return HookResult()

    async def _run_http(self, hook: LifecycleHook, context: HookContext) -> HookResult:
        """POST to URL with event payload, parse response."""
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available for HTTP hook")
            return HookResult()

        payload = {
            "event": context.event.value,
            "agent_id": context.agent_id,
            "run_id": context.run_id,
            "tool_name": context.tool_name,
            "tool_args": context.tool_args,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    hook.handler,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            action = HookAction(data.get("action", "allow"))
                        except ValueError:
                            action = HookAction.ALLOW
                        return HookResult(
                            action=action,
                            modified_args=data.get("modified_args"),
                            reason=data.get("reason", ""),
                            system_message=data.get("system_message", ""),
                        )
                    logger.warning("HTTP hook returned %d", resp.status)
                    return HookResult()
        except Exception as e:
            logger.error("HTTP hook failed: %s", e)
            return HookResult()

    async def _run_agent(self, hook: LifecycleHook, context: HookContext) -> HookResult:
        """Execute an agent as a hook handler via the runner."""
        try:
            from robothor.engine.tools.handlers.spawn import get_runner
        except ImportError:
            logger.error("Cannot import get_runner for agent hook %s", hook.handler)
            return HookResult()

        runner = get_runner()
        if runner is None:
            logger.warning("No runner available for agent hook %s", hook.handler)
            return HookResult()

        context_dict = {
            "event": context.event.value,
            "agent_id": context.agent_id,
            "run_id": context.run_id,
            "tool_name": context.tool_name,
            "tool_args": context.tool_args,
        }

        try:
            run = await runner.execute(
                agent_id=hook.handler,
                message=json.dumps(context_dict),
                trigger_type="sub_agent",
            )

            if run.output_text:
                try:
                    data = json.loads(run.output_text)
                    action = HookAction(data.get("action", "allow"))
                    return HookResult(
                        action=action,
                        modified_args=data.get("modified_args"),
                        reason=data.get("reason", ""),
                        system_message=data.get("system_message", ""),
                    )
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "Agent hook %s output not valid JSON, allowing",
                        hook.handler,
                    )
                    return HookResult()

            return HookResult()

        except Exception as e:
            logger.error("Agent hook %s failed: %s", hook.handler, e)
            return HookResult()


# ─── Manifest loading ────────────────────────────────────────────────


def load_hooks_from_manifest(
    manifest: dict[str, Any],
    agent_id: str,
) -> list[LifecycleHook]:
    """Parse lifecycle_hooks from an agent manifest's v2 block."""
    v2 = manifest.get("v2", {})
    raw_hooks = v2.get("lifecycle_hooks", [])
    hooks = []

    for raw in raw_hooks:
        if not isinstance(raw, dict):
            continue
        try:
            event = HookEvent(raw.get("event", ""))
        except ValueError:
            logger.warning("Unknown hook event %r in agent %s", raw.get("event"), agent_id)
            continue

        hooks.append(
            LifecycleHook(
                event=event,
                handler_type=raw.get("handler_type", "python"),
                handler=raw.get("handler", ""),
                blocking=raw.get("blocking", False),
                priority=int(raw.get("priority", 100)),
                filter=raw.get("filter", {}),
                scope=raw.get("scope", "agent"),
                agent_id=agent_id,
                chain_mode=raw.get("chain_mode", "short_circuit"),
                timeout=int(raw.get("timeout", 30)),
            )
        )

    return hooks


def load_global_hooks(hooks_dir: Any) -> list[LifecycleHook]:
    """Load global hook definitions from a YAML file."""
    from pathlib import Path

    hooks_dir = Path(hooks_dir)
    global_file = hooks_dir / "global.yaml"
    if not global_file.exists():
        return []

    try:
        with global_file.open() as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("Failed to load global hooks: %s", e)
        return []

    raw_hooks = data.get("hooks", [])
    hooks = []

    for raw in raw_hooks:
        if not isinstance(raw, dict):
            continue
        try:
            event = HookEvent(raw.get("event", ""))
        except ValueError:
            continue

        hooks.append(
            LifecycleHook(
                event=event,
                handler_type=raw.get("handler_type", "python"),
                handler=raw.get("handler", ""),
                blocking=raw.get("blocking", False),
                priority=int(raw.get("priority", 100)),
                filter=raw.get("filter", {}),
                scope="global",
                agent_id="",
                chain_mode=raw.get("chain_mode", "short_circuit"),
                timeout=int(raw.get("timeout", 30)),
            )
        )

    return hooks


# ─── Singleton ───────────────────────────────────────────────────────

_hook_registry: HookRegistry | None = None


def get_hook_registry() -> HookRegistry | None:
    """Get the hook registry singleton."""
    return _hook_registry


def init_hook_registry() -> HookRegistry:
    """Initialize the hook registry singleton."""
    global _hook_registry
    _hook_registry = HookRegistry()
    return _hook_registry
