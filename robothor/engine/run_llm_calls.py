"""LLM call recording + thin dispatch delegators for AgentRunner.

Extracted from runner.py 2026-08-24 (phase 2 of the god-object
decomposition). CONTRACT: methods here may use ``self._llm`` (the LLMClient),
``self.config``, each other, and the lifecycle mixin's ``_active_watchdog``
(the composed class provides it); nothing else from AgentRunner. The real
dispatch/cost/streaming logic lives in llm_client.LLMClient — this mixin is
the runner-facing recording wrapper plus the historical method surface that
hundreds of call sites and tests patch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.config import EngineConfig

    from robothor.engine.session import AgentSession


import contextlib
import logging
import re
import time
from typing import Any

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.llm_client import LLMClient  # noqa: E402

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.sanitize import sanitize_log as _sanitize

#: Tools whose work is several sub-agent runs, so the agent-level per-tool cap
#: (120s by default) is far too short. Kept at a 600s floor.
_LONG_RUNNING_TOOLS = frozenset(
    {
        "benchmark_run",
        "benchmark_run_fleet",
        "benchmark_run_for_agent",
        "benchmark_compare",
        "experiment_measure",
        "spawn_agent",
        "spawn_agents",
        # Measured 2026-08-22 against 14 days of real calls: each of these died
        # at exactly the 120s default and NONE ever completed above it.
        # buddy_review_pass 8 of 10 (main had no buddy review since 08-19,
        # vision-monitor since 08-17), deep_reason 4 of 18, look 3 of 70.
        # detectors.find_tools_capped_at_timeout reports the next one.
        "buddy_review_pass",
        "deep_reason",
        "look",
    }
)

#: Of those, the tools that already enforce their OWN per-task budget: the
#: benchmark harness caps each case at the suite's ``timeout_seconds:`` (or 900s
#: by default) and records an overrun as a timeout rather than a grade. A second,
#: smaller cap out here can only cut a case short *below* the budget its suite
#: declared, and the run is then filed against the agent.
#:
#: This list previously named ``benchmark_run`` only, while the tools the fleet
#: grader actually calls are ``benchmark_run_fleet`` and
#: ``benchmark_run_for_agent`` -- so the two tools that run every benchmark
#: inherited the 120s default. Measured 2026-08-22: agent-architect
#: ``fleet-analysis`` had never once completed above 120.0s across 91 completed
#: runs, against a 512s production mean with zero production timeouts.
_HARNESS_BUDGETED_TOOLS = frozenset(
    {
        "benchmark_run",
        "benchmark_run_fleet",
        "benchmark_run_for_agent",
    }
)


#: How stale an interactive preamble may be before the next turn re-warms.
#: The old gate was "history is empty", which never fires on a persistent
#: session: main.yaml sets session_target: persistent and that session holds
#: 5,560 messages. Measured over 30 days, cron runs executed 11.0 warmup
#: sections each while telegram runs executed 0.0 — the operator's own
#: conversations loaded no memory blocks, preferences or breadcrumbs at all.
INTERACTIVE_WARMUP_MAX_AGE_S = 900


def _seconds_since_last_interactive_run(agent_id: str, tenant_id: str) -> float | None:
    """Seconds since this agent's previous interactive run, or None if there is none.

    Best-effort: on any error the caller warms, which is the safe direction —
    a redundant preamble costs latency, a missing one costs the operator their
    memory context.
    """
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))
                FROM agent_runs
                WHERE agent_id = %s AND tenant_id = %s
                  AND trigger_type IN ('telegram', 'webchat')
                """,
                (agent_id, tenant_id),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:  # noqa: BLE001 — never block a turn on this
        logger.debug("interactive warmup recency lookup failed: %s", _sanitize(exc))
        return None


def should_warm_interactive(*, history_len: int, seconds_since_warmup: float | None) -> bool:
    """Whether an interactive turn should build the warmup preamble.

    First turn of a session always warms. After that, warm again once the last
    preamble is older than ``INTERACTIVE_WARMUP_MAX_AGE_S`` — a conversation
    resumed hours later gets fresh memory, a rapid back-and-forth does not pay
    for it on every turn.

    The old comment claimed follow-ups inherit memory blocks from conversation
    history. They do not: the preamble is prepended to a local variable and
    never persisted to the session, so there is nothing for a follow-up to
    inherit.
    """
    if history_len <= 0:
        return True
    if seconds_since_warmup is None:
        return True
    return seconds_since_warmup > INTERACTIVE_WARMUP_MAX_AGE_S


def _resolve_tool_timeout(tool_name: str, configured: int) -> int:
    """Per-tool wall-clock cap, in seconds. 0 means unlimited.

    One owner per budget: where the callee already bounds its own work, this
    layer must not impose a second, smaller bound.
    """
    if tool_name in _HARNESS_BUDGETED_TOOLS:
        return 0
    if tool_name in _LONG_RUNNING_TOOLS:
        return max(configured, 600)
    return configured


def _normalize_model_id(model: str) -> str:
    """Collapse a model id to a provider/format-agnostic core for comparison.

    litellm reports `response.model` without the `openrouter/` prefix and often
    with a trailing date or dashes-for-dots, so an exact string compare against
    the manifest's `model_primary` would false-positive on a *healthy* run. We
    take the last path segment, drop a trailing date, and strip separators so
    `openrouter/anthropic/claude-opus-4.7` and `claude-opus-4-7-20260416`
    compare equal while still distinguishing genuinely different models.
    """
    core = (model or "").strip().lower().rsplit("/", 1)[-1]
    core = re.sub(r"[-_]?\d{6,}$", "", core)  # trailing date/build stamp
    return re.sub(r"[.\-_\s]", "", core)


# Announce-mode runs that end with fewer characters than this are flagged
# as "partial" — almost always a meta-confirmation ("briefing delivered")
# rather than the real content the agent was supposed to broadcast.
ANNOUNCE_MIN_OUTPUT_CHARS = 200

# Init timeout: max seconds for agent setup before first LLM call.
# Agents that hang during warmup, adapter loading, or tool registration
# are killed immediately.  Prevents the "stuck in initialization"
# failure mode where runs sit for 30+ minutes with 0 tokens consumed.
INIT_TIMEOUT_SECONDS = 60

# Defined before the env-tunable constants below: _int_env_with_fallback and
# the soft>=hard sanity check run AT IMPORT TIME, so the logger must already
# exist on a fresh import (reload-based tests mask this — the old module dict
# keeps a stale binding alive).

logger = logging.getLogger(__name__)


class LLMCallMixin:
    """See module docstring for the contract."""

    if TYPE_CHECKING:
        # Provided by the composed AgentRunner — the mixin contract's whole
        # allowed surface, declared so mypy checks it and nothing more.
        config: EngineConfig
        _llm: LLMClient

        @property
        def _active_watchdog(self) -> Any: ...

    async def _llm_call_and_record(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        trace: Any = None,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[Any, str, int, dict[str, Any]]:
        """Make an LLM call, record it in session, return (response, model, ms, msg_dict)."""
        start = time.monotonic()

        if trace:
            with trace.span("llm_call") as _span:
                response = await self._do_llm_call(
                    session,
                    models,
                    tool_schemas,
                    on_content,
                    broken_models,
                    temperature,
                    on_stream_event=on_stream_event,
                )
                # GenAI semantic-convention attributes for OTel export.
                if response is not None:
                    with contextlib.suppress(Exception):
                        from robothor.engine.telemetry import gen_ai_attributes

                        _usage = getattr(response, "usage", None)
                        _finish = ""
                        if getattr(response, "choices", None):
                            _finish = getattr(response.choices[0], "finish_reason", "") or ""
                        _span.attributes.update(
                            gen_ai_attributes(
                                model=getattr(response, "model", None)
                                or (models[0] if models else ""),
                                input_tokens=getattr(_usage, "prompt_tokens", 0) or 0,
                                output_tokens=getattr(_usage, "completion_tokens", 0) or 0,
                                finish_reason=_finish,
                            )
                        )
        else:
            response = await self._do_llm_call(
                session,
                models,
                tool_schemas,
                on_content,
                broken_models,
                temperature,
                on_stream_event=on_stream_event,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        # Touch stall watchdog — LLM responded, we're alive
        if self._active_watchdog:
            model_name = getattr(response, "model", None) or (models[0] if models else "unknown")
            self._active_watchdog.touch(f"llm_response:{model_name}")

        if response is None or not response.choices:
            return response, "", elapsed_ms, {}

        choice = response.choices[0]
        assistant_msg = choice.message
        model_used = response.model or models[0]

        # Build assistant message dict — filter thinking blocks from output text
        msg_dict: dict[str, Any] = {"role": "assistant"}
        raw_content = assistant_msg.content
        if isinstance(raw_content, list):
            # Response contains content blocks (e.g. thinking + text)
            # Keep full blocks in message for conversation continuity;
            # get_final_text() filters thinking blocks when extracting output
            msg_dict["content"] = raw_content
        else:
            if raw_content:
                msg_dict["content"] = raw_content
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]

        # Record LLM call — extract standard + cache token counts
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

        # Cache tokens: Anthropic exposes these directly; other providers
        # may use prompt_tokens_details.cached_tokens instead
        cache_creation_tokens = (
            (getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage else 0
        )
        cache_read_tokens = (getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
        if usage and not cache_read_tokens:
            details = getattr(usage, "prompt_tokens_details", None)
            if details:
                cache_read_tokens = getattr(details, "cached_tokens", 0) or 0

        session.record_llm_call(
            model=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            duration_ms=elapsed_ms,
            assistant_message=msg_dict,
        )

        # Best-effort cost tracking (cache-aware fallback)
        cost = self._response_cost(
            response=response,
            model_used=model_used,
            models=models,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        session.run.total_cost_usd += cost

        # Record step cost for projection (used by hard budget pre-flight check)
        session.record_step_cost(cost or 0.0)

        return response, model_used, elapsed_ms, msg_dict

    # ── LLM dispatch / cost — extracted to llm_client.LLMClient (Slice 1) ──
    # Thin delegators preserve the historical AgentRunner method surface for
    # existing call sites and tests; implementations live on ``self._llm``.

    def _response_cost(self, **kwargs: Any) -> float:
        return self._llm._response_cost(**kwargs)

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        return self._llm._calculate_cost(
            model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
        )

    async def _do_llm_call(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
        return await self._llm._do_llm_call(
            session,
            models,
            tool_schemas,
            on_content,
            broken_models,
            temperature,
            on_stream_event=on_stream_event,
        )

    # ─── Error Recovery Helper ──────────────────────────────────────

    async def _prepare_llm_call(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        broken_models: set[str] | None = None,
    ) -> int:
        return await self._llm._prepare_llm_call(messages, models, broken_models)

    # Pure-static helpers — aliased to the extracted implementations so
    # ``AgentRunner._validate_tool_pairs(...)`` etc. keep resolving.
    _validate_tool_pairs = staticmethod(LLMClient._validate_tool_pairs)
    _guard_trailing_assistant = staticmethod(LLMClient._guard_trailing_assistant)
    _build_llm_kwargs = staticmethod(LLMClient._build_llm_kwargs)

    # Pure-static helper — aliased to the extracted implementation.
    _handle_model_error = staticmethod(LLMClient._handle_model_error)

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        return await self._llm._call_llm(
            messages, models, tools, broken_models=broken_models, temperature=temperature
        )

    async def _call_llm_streaming(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None = None,
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
        return await self._llm._call_llm_streaming(
            messages,
            models,
            tools,
            on_content,
            broken_models=broken_models,
            temperature=temperature,
            on_stream_event=on_stream_event,
        )
