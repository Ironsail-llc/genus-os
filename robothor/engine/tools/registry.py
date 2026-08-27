"""Tool Registry — schema filtering + execution for the Agent Engine."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import (
    CORE_TOOLS,
    GOAL_TOOLS,
    SPAWN_TOOLS,
    TODO_TOOLS,
    TOOLSEARCH_TOOLS,
)
from robothor.engine.tools.dispatch import _execute_tool
from robothor.engine.tools.schemas import get_engine_schemas

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of available tools with schema filtering per agent."""

    # Warn-once dedup for unresolved-tool warnings, keyed by agent id. Class-level
    # (shared across instances) so re-instantiated registries — the sub-agent
    # runner and template validator each build a fresh ToolRegistry() — don't
    # degrade the "warn once" guarantee into "warn every build".
    _warned_unresolved: set[str] = set()

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        #: Names contributed by plugins, so a reload can withdraw them again.
        self._plugin_schema_names: set[str] = set()
        #: Plugin generation `_schemas` reflects; -1 until first registration.
        self._schema_generation: int = -1
        self._adapter_routes: dict[str, str] = {}  # tool_name → adapter server name
        self._register_all()

    @classmethod
    def reset_unresolved_warnings(cls) -> None:
        """Clear the process-wide warn-once dedup (test isolation hook)."""
        cls._warned_unresolved.clear()

    def _register_all(self) -> None:
        """Register all tool schemas."""
        from robothor.api.mcp import get_tool_definitions

        # MCP tools
        for defn in get_tool_definitions():
            name = defn["name"]
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": defn["description"],
                    "parameters": defn["inputSchema"],
                },
            }

        # Engine-specific tools
        self._schemas.update(get_engine_schemas())

        # Plugin-contributed schemas. Without this the seam is half-wired:
        # dispatch.py registers a plugin's HANDLER, so the tool can be
        # executed, but `_get_filtered_names` filters tools_allowed by
        # membership in `_schemas`, so a plugin tool named in a manifest was
        # routed to the "silently unavailable" branch and never advertised to
        # the model. docs/PLUGINS.md promised the opposite in plain words, and
        # the seam's own tests asserted against the loader's dataclass rather
        # than the registry, so they passed while nothing worked.
        #
        # Built-ins are passed as reserved so a plugin cannot shadow `exec`
        # or `write_file` — a takeover, not an extension. The reserved set is
        # taken AFTER the built-ins are registered, so it is the real one.
        self._register_plugin_schemas()

    def _refresh_plugin_schemas_if_stale(self) -> None:
        """Re-read plugin schemas when the plugin generation has moved.

        Installing a plugin used to require a restart before the model was
        told the tool existed. The loader's generation counter marks this
        cache stale; the names a plugin contributed are tracked so they can
        be withdrawn again when the plugin goes away, which a plain re-add
        would never do.
        """
        from robothor.plugins import generation

        current = generation()
        if current == self._schema_generation:
            return
        for name in self._plugin_schema_names:
            self._schemas.pop(name, None)
        self._plugin_schema_names = set()
        self._register_plugin_schemas()

    def _register_plugin_schemas(self) -> None:
        """Advertise plugin tools to the model. Never raises: one broken
        package must not stop the engine from starting."""
        try:
            from robothor.plugins import load_plugins

            plugins = load_plugins(reserved_names=set(self._schemas))
        except Exception as e:  # noqa: BLE001 - a plugin must not break boot
            logger.warning("Plugin schema registration skipped: %s", e)
            return

        from robothor.plugins import generation

        self._schema_generation = generation()
        registered: list[str] = []
        for name, schema in (plugins.schemas or {}).items():
            if name in self._schemas:
                logger.warning(
                    "Plugin schema %r refused — it would shadow a built-in tool",
                    name,
                )
                continue
            self._schemas[name] = schema
            self._plugin_schema_names.add(name)
            registered.append(name)

        # A handler with no declared schema still has to be advertised, or the
        # documented quickstart is false: docs/PLUGINS.md's example contributes
        # only `handlers` and then promises the tool "is available to any agent
        # whose manifest lists it in tools_allowed". Synthesize a permissive
        # schema from the handler's docstring so that promise holds. An
        # explicit schema always wins.
        for name, handler in (plugins.tools or {}).items():
            if name in self._schemas:
                continue
            doc = (getattr(handler, "__doc__", "") or "").strip().split("\n")[0]
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc or f"Plugin tool {name}.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            registered.append(name)

        if registered:
            logger.info(
                "Registered %d plugin tool schema(s): %s",
                len(registered),
                ", ".join(sorted(registered)),
            )

    # Adapter connection failure cache: {adapter_name: (fail_time, backoff_seconds)}
    _adapter_failures: dict[str, tuple[float, float]] = {}

    async def register_adapter_tools(self, adapters: list[Any]) -> None:
        """Connect to adapter MCP servers, discover tools, register as first-class schemas.

        Resilience features:
        - 5s timeout per adapter connection (don't block agent startup)
        - Failed adapters cached with exponential backoff (5min initial, 30min max)
        - Failures logged once at WARNING, then suppressed until retry window
        """
        import asyncio
        import time

        from robothor.engine.adapters import verify_adapter_integrity
        from robothor.engine.mcp_client import get_mcp_client_pool

        pool = get_mcp_client_pool()
        for adapter in adapters:
            # Integrity BEFORE connection: an unverified stdio binary must not
            # even be spawned, let alone asked for its tool list.
            ok, reason = verify_adapter_integrity(adapter)
            if not ok:
                logger.error(
                    "Adapter '%s' REFUSED — integrity check failed: %s",
                    adapter.name,
                    reason,
                )
                continue
            # Check failure cache — skip if in backoff window
            now = time.monotonic()
            if adapter.name in self._adapter_failures:
                fail_time, backoff = self._adapter_failures[adapter.name]
                if now - fail_time < backoff:
                    logger.debug(
                        "Adapter '%s': skipping (backoff %.0fs remaining)",
                        adapter.name,
                        backoff - (now - fail_time),
                    )
                    continue

            try:
                session = await asyncio.wait_for(pool.get_session(adapter.name), timeout=5.0)
                mcp_tools = await asyncio.wait_for(session.list_tools(), timeout=5.0)
                allow = set(adapter.tools_allowed)
                if not allow:
                    logger.warning(
                        "Adapter '%s' has no tools_allowed list — registering "
                        "every tool its server offers (legacy allow-all). Pin "
                        "the expected tools in the adapter YAML so a changed "
                        "server cannot sprout new capabilities silently.",
                        adapter.name,
                    )
                drifted: list[str] = []
                for tool in mcp_tools:
                    name = tool.get("name", "")
                    if not name:
                        continue
                    if allow and name not in allow:
                        drifted.append(name)
                        continue
                    self._schemas[name] = {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": tool.get("description", ""),
                            "parameters": tool.get(
                                "inputSchema", {"type": "object", "properties": {}}
                            ),
                        },
                    }
                    self._adapter_routes[name] = adapter.name
                if drifted:
                    # Loud on purpose: the server offered tools the bundle
                    # never declared. That is the exact supply-chain move —
                    # a compromised or auto-updated server growing new
                    # capabilities — and it must read as an event, not debug.
                    logger.warning(
                        "Adapter '%s' DRIFT: server offered %d tool(s) not in "
                        "tools_allowed, refused: %s",
                        adapter.name,
                        len(drifted),
                        ", ".join(sorted(drifted)),
                    )
                # Clear failure cache on success
                self._adapter_failures.pop(adapter.name, None)
                logger.info("Adapter '%s': discovered %d tools", adapter.name, len(mcp_tools))
            except Exception as e:
                # Exponential backoff: 300s (5min) -> 600s -> 1200s -> max 1800s (30min)
                _, prev_backoff = self._adapter_failures.get(adapter.name, (0, 150.0))
                new_backoff = min(prev_backoff * 2, 1800.0)
                self._adapter_failures[adapter.name] = (now, new_backoff)
                if prev_backoff <= 150.0:
                    # First failure or first retry — log at WARNING
                    logger.warning(
                        "Adapter '%s' unavailable (backoff %.0fs): %s",
                        adapter.name,
                        new_backoff,
                        e,
                    )
                else:
                    logger.debug(
                        "Adapter '%s' still unavailable, backoff %.0fs", adapter.name, new_backoff
                    )

    def get_adapter_route(self, tool_name: str) -> str | None:
        """Return the adapter server name for a tool, or None if not adapter-provided."""
        return self._adapter_routes.get(tool_name)

    def build_for_agent(self, config: AgentConfig) -> list[dict[str, Any]]:
        """Return filtered tool schemas for an agent based on allow/deny lists.

        When deferral (Rip 16 / G4) is active for this agent, advertise only the
        CORE_TOOLS subset plus the tool_search/tool_describe/tool_call meta-tools;
        the agent's other allowed tools load on demand. Enforcement of the real
        allow-list lives in the tool_call/tool_describe handlers, which check the
        ``_deferred_allowed`` set the runner publishes via ``set_deferred_allowed``
        (see deferred_whitelist) — so tool_call cannot reach a denied tool.
        """
        self._refresh_plugin_schemas_if_stale()
        names = self._get_filtered_names(config)
        if self.should_defer(config):
            seen: set[str] = set()
            advertised: list[str] = []
            for n in [*names, *sorted(TOOLSEARCH_TOOLS)]:
                in_core = n in CORE_TOOLS or n in TOOLSEARCH_TOOLS
                if in_core and n in self._schemas and n not in seen:
                    seen.add(n)
                    advertised.append(n)
            return [self._schemas[n] for n in advertised]
        return [self._schemas[n] for n in names]

    def should_defer(self, config: AgentConfig) -> bool:
        """True iff this agent's toolset should be deferred (Rip 16 / G4).

        Deferral kicks in only for broad-access agents — those whose advertised
        tool count exceeds the threshold — so curated small-toolset workers keep
        their full set with no extra tool_search round-trip.
        """
        from robothor.engine.feature_flags import (
            deferred_tools_enabled,
            deferred_tools_threshold,
        )

        if not deferred_tools_enabled():
            return False
        return len(self._get_filtered_names(config)) > deferred_tools_threshold()

    def deferred_whitelist(self, config: AgentConfig) -> frozenset[str]:
        """The deferred-run allow-set the runner publishes via set_deferred_allowed.

        The agent's full allowed set plus the meta-tools. tool_describe/tool_call
        check membership in this set (the _deferred_allowed ContextVar), so a
        direct CORE call or a tool_call to any allowed tool passes, while a
        tool_call to a denied tool is refused before registry.execute.
        """
        return frozenset(self._get_filtered_names(config)) | TOOLSEARCH_TOOLS

    def search_tools(self, names: Any, query: str, limit: int = 10) -> list[dict[str, str]]:
        """Rank a set of tool names against a free-text query.

        Returns ``[{"name", "description"}]`` for the top matches. Used by the
        tool_search meta-tool, which passes the agent's allow-set (the runner's
        _deferred_allowed set) so search is scoped to what the agent may run.
        """
        terms = [t for t in query.lower().split() if t]
        scored: list[tuple[int, str, str]] = []
        for n in names:
            schema = self._schemas.get(n, {})
            fn = schema.get("function", {})
            desc = str(fn.get("description", ""))
            haystack = f"{n} {desc}".lower()
            if not terms:
                score = 0
            else:
                score = sum(haystack.count(t) for t in terms)
                # Strong boost when the tool name itself matches a term.
                score += sum(5 for t in terms if t in n.lower())
            if score > 0 or not terms:
                scored.append((score, n, desc[:200]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{"name": n, "description": d} for _s, n, d in scored[: max(1, limit)]]

    def get_schema(self, name: str) -> dict[str, Any] | None:
        """Return the full OpenAI-function schema for one tool, or None."""
        self._refresh_plugin_schemas_if_stale()
        return self._schemas.get(name)

    def _readonly_names(self) -> set[str]:
        """Core's table plus whatever installed plugins declared about their own tools.

        A plugin ships handlers and schemas; before this, safety
        classification stayed hardcoded here, so extracting an integration to
        a plugin left a fact about that instance behind in core. A plugin that
        declares nothing contributes nothing — absent means WRITE, which is
        the safe default.
        """
        from robothor.engine.tools.constants import READONLY_TOOLS

        names = set(READONLY_TOOLS)
        try:
            from robothor.plugins import load_plugins

            names |= load_plugins(reserved_names=set()).read_only
        except Exception as e:  # noqa: BLE001 - a plugin must not break plan mode
            logger.warning("Plugin read-only declarations skipped: %s", e)
        return names

    def build_readonly_for_agent(self, config: AgentConfig) -> list[dict[str, Any]]:
        """Return only read-only tool schemas for plan mode."""
        self._refresh_plugin_schemas_if_stale()
        full_names = set(self.get_tool_names(config))
        readonly_names = sorted(full_names & self._readonly_names())
        return [self._schemas[n] for n in readonly_names if n in self._schemas]

    def get_readonly_tool_names(self, config: AgentConfig) -> list[str]:
        """Return read-only tool names for plan mode."""
        full_names = set(self.get_tool_names(config))
        return sorted(full_names & self._readonly_names())

    def get_tool_names(self, config: AgentConfig) -> list[str]:
        """Return filtered tool names for an agent."""
        return self._get_filtered_names(config)

    def _get_filtered_names(self, config: AgentConfig) -> list[str]:
        if config.tools_allowed:
            names = [n for n in config.tools_allowed if n in self._schemas]
            unresolved = [n for n in config.tools_allowed if n not in self._schemas]
            if unresolved and config.id not in self._warned_unresolved:
                self._warned_unresolved.add(config.id)
                logger.warning(
                    "Agent %s declares %d tool(s) with no registered schema/adapter "
                    "route; they are silently unavailable: %s",
                    config.id,
                    len(unresolved),
                    ", ".join(sorted(unresolved)),
                )
                self._escalate_unresolved(config.id, sorted(unresolved))
            names.extend(n for n in GOAL_TOOLS if n in self._schemas and n not in names)
        else:
            names = list(self._schemas.keys())

        if config.tools_denied:
            # Support glob patterns (e.g. "mcp_*", "gws_*") in tools_denied
            has_globs = any(c in p for p in config.tools_denied for c in "*?[")
            if has_globs:
                from fnmatch import fnmatch

                names = [n for n in names if not any(fnmatch(n, p) for p in config.tools_denied)]
            else:
                denied = set(config.tools_denied)
                names = [n for n in names if n not in denied]

        # Exclude spawn tools unless agent has can_spawn_agents enabled
        if not config.can_spawn_agents:
            names = [n for n in names if n not in SPAWN_TOOLS]

        # Exclude todo list tools unless agent has todo_list_enabled
        if not config.todo_list_enabled:
            names = [n for n in names if n not in TODO_TOOLS]

        # Meta-tools (tool_search/tool_describe/tool_call) are never part of the
        # normal advertised set; build_for_agent injects them only when deferring.
        if TOOLSEARCH_TOOLS:
            names = [n for n in names if n not in TOOLSEARCH_TOOLS]

        return names

    @staticmethod
    def _escalate_unresolved(agent_id: str, unresolved: list[str]) -> None:
        """Escalate declared-but-unresolvable manifest tools to the operator.

        This is operator-actionable instance drift — the manifest names a tool
        the platform does not register — and the journald warning alone has
        proven invisible (months of unactioned warnings). Fire-and-forget at
        warning level, deduped per agent per process by the caller's warn-once
        set. Degrades to log-only when no event loop is running (CLI, template
        validation, tests).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        from robothor.engine import alerts
        from robothor.engine.task_registry import get_task_registry

        get_task_registry().spawn(
            alerts.alert(
                "warning",
                f"Agent '{agent_id}' declares unavailable tools",
                "Declared in the manifest but not registered — silently "
                "unavailable to the agent: " + ", ".join(unresolved) + ". "
                "Fix the manifest, or register the missing tool schema.",
            ),
            name=f"tool-drift-alert:{agent_id}",
        )

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str = "",
        run_id: str = "",
        tenant_id: str = "",
        workspace: str = "",
        user_id: str = "",
        user_role: str = "",
        accessible_tenant_ids: tuple[str, ...] = (),
        timeout: int = 120,
        task_author_override: str = "",
        is_benchmark: bool = False,
        identity: IdentityContext | None = None,
    ) -> dict[str, Any]:
        """Execute a tool and return the result dict.

        Args:
            timeout: Per-tool timeout in seconds. 0 = unlimited.
            accessible_tenant_ids: Tenant IDs this run may access
                (resolved from user role + tenant hierarchy).
            is_benchmark: When True, side-effect tool wrappers refuse
                mutations (see ToolContext.is_benchmark).
            identity: The run's resolved IdentityContext (Task 2), or None
                for system/cron/heartbeat runs. Threaded onto ToolContext for
                data-scoping (Task 5); every existing caller omitting this
                gets the unaffected default.
        """
        # Match supplied argument names to this tool's declared parameters,
        # ignoring case and underscores. 82 tools are snake_case and 26 are
        # camelCase, so a model that has just called get_task({"id": ...})
        # sends `to_agent` to a tool declaring `toAgent` — and gets a generic
        # failure with nothing naming the wrong key. Exact matches win;
        # ambiguity is left alone. See normalise_arguments.
        from robothor.engine.tools.dispatch import normalise_arguments

        # getattr, not self._schemas: this method is exercised with stub
        # registries that never build a schema map, and argument normalisation
        # must never be the reason a tool call fails.
        _schemas = getattr(self, "_schemas", None) or {}
        _props = (
            ((_schemas.get(tool_name) or {}).get("function") or {})
            .get("parameters", {})
            .get("properties")
        )
        arguments = normalise_arguments(arguments, _props)

        try:
            if timeout > 0:
                async with asyncio.timeout(timeout):
                    return await _execute_tool(
                        tool_name,
                        arguments,
                        agent_id=agent_id,
                        run_id=run_id,
                        tenant_id=tenant_id,
                        workspace=workspace,
                        user_id=user_id,
                        user_role=user_role,
                        accessible_tenant_ids=accessible_tenant_ids,
                        task_author_override=task_author_override,
                        is_benchmark=is_benchmark,
                        identity=identity,
                    )
            else:
                return await _execute_tool(
                    tool_name,
                    arguments,
                    agent_id=agent_id,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    workspace=workspace,
                    user_id=user_id,
                    user_role=user_role,
                    accessible_tenant_ids=accessible_tenant_ids,
                    task_author_override=task_author_override,
                    is_benchmark=is_benchmark,
                    identity=identity,
                )
        except TimeoutError:
            logger.warning("Tool %s timed out after %ds", tool_name, timeout)
            return {
                "error": f"Tool '{tool_name}' timed out after {timeout}s. "
                "Try a different approach or skip this step."
            }
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return {"error": f"Tool execution failed: {e}"}


# Singleton
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Get or create the singleton tool registry."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
