"""Run finalization — everything that happens after the loop stops.

Extracted from runner.py on 2026-08-24 (phase 1 of the god-object
decomposition; runner.py was 4,660 lines with a 1,097-line execute and a
1,227-line _run_loop). This cluster was chosen first because the coupling
analysis showed it is already clean: its only dependency on the rest of the
runner is ``self.config`` — zero calls to any method outside this file.

CONTRACT: methods in this mixin may use ``self.config`` and each other,
nothing else from AgentRunner. A change that adds a new ``self.`` dependency
is expanding the god-object again — put the dependency on the method
signature instead. (Mixin rather than composition ON PURPOSE for phase 1:
hundreds of tests patch these methods on AgentRunner, and behavior-preserving
extraction beats a big-bang interface change. Phase 2 converts to composition
once call sites pass state explicitly.)

Covers: post-run guardrails and delivery hooks, primary-model-reached
detection, telemetry publication, claim/metric verification, outcome
assessment, DB persistence (async spawn + sync fallback), and task-state
update.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.session import AgentSession

import asyncio
import contextlib
import functools
import logging
import os
import re
from typing import Any

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.models import (
    AgentRun,
    DeliveryMode,
)

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.sanitize import sanitize_log as _sanitize  # noqa: E402
from robothor.engine.tracking import create_step, create_steps_batch, update_run

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


def _escalate_unfinished_todos(
    todos: Any,
    parent_task_id: str | None,
    agent_id: str,
    tenant_id: str = "",
    agent_config: Any = None,
    run_id: str | None = None,
) -> bool:
    """Lift unfinished todo_write items back to the CRM parent task so the
    next heartbeat's planner picks up where this run left off.

    Closes the Stage-5 "full circle" gap: a run that exhausted iterations
    with items still pending or in_progress must propagate that work to
    the thread pool — silently dropping it is the prior failure mode.

    Behavior:
      - None/empty todo list → no-op, returns False.
      - No parent_task_id → no-op (list was standalone).
      - All items completed → no-op (run finished cleanly).
      - Otherwise: write set_next_action("Continue: <first_unfinished>")
        on the parent. If the parent isn't already tagged `thread`, add
        the tag and seed `objective` from the title if empty. Non-
        destructive — never touches status, owner, or other tags.

    Phase 3 addition: when ``agent_config.todo_list_enabled`` and
    ``agent_config.task_protocol`` are both true AND the
    ``ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED`` env is "1", unfinished
    items are ALSO promoted to real CRM subtasks (idempotent via content
    hash). The ``next_action`` write above stays — promotion is additive.
    """
    if todos is None:
        return False
    items = getattr(todos, "items", None) or []
    if not items:
        return False
    if not parent_task_id:
        return False

    unfinished = [it for it in items if getattr(it, "status", "") in ("pending", "in_progress")]
    if not unfinished:
        return False

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal

    effective_tenant = tenant_id or DEFAULT_TENANT

    try:
        parent = dal.get_task(parent_task_id, tenant_id=effective_tenant)
    except Exception as e:
        logger.warning("todo escalation: get_task failed for %s: %s", _sanitize(parent_task_id), e)
        return False
    if not parent:
        return False

    first = unfinished[0]
    next_action = f"Continue: {getattr(first, 'content', '').strip()}"[:500]

    dal.set_next_action(
        task_id=parent_task_id,
        next_action=next_action,
        agent=agent_id,
        by="runtime",
        tenant_id=effective_tenant,
    )

    existing_tags = parent.get("tags") or []
    if "thread" not in existing_tags:
        new_tags = list(existing_tags) + ["thread"]
        update_kwargs: dict[str, Any] = {
            "task_id": parent_task_id,
            "tags": new_tags,
            "changed_by": agent_id,
            "tenant_id": effective_tenant,
        }
        # Seed objective from title if empty — the planner needs something
        # to plan against on the next beat.
        if not (parent.get("objective") or "").strip():
            update_kwargs["objective"] = parent.get("title") or ""
        dal.update_task(**update_kwargs)
        # Refresh the local parent dict so the promotion step below sees the
        # freshly added `thread` tag.
        parent["tags"] = new_tags

    # Phase 3 — promote unfinished items to real subtasks (best-effort).
    if agent_config is not None:
        try:
            from robothor.engine.todo_promotion import promote_unfinished_items

            promote_unfinished_items(
                parent=parent,
                items=unfinished,
                agent_config=agent_config,
                agent_id=agent_id,
                run_id=run_id or "",
                tenant_id=effective_tenant,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("todo promotion error (non-fatal): %s", _sanitize(e))

    return True


class RunFinalizationMixin:
    """Post-run finalization for AgentRunner. See module docstring for the contract."""

    config: EngineConfig

    def _after_response_delivered(
        self,
        session: AgentSession,
        run: AgentRun,
    ) -> None:
        """Post-response hook. Called from _finish_run before return.

        Default behaviour (Rip 1): when ``ROBOTHOR_RIP_1_ENABLED=1``
        and the session's nudge counters have tripped, schedule the
        background-review fork as a non-blocking asyncio task. The
        fork runs concurrently while ``_finish_run`` finishes
        persisting the foreground response.

        The hook is sync because ``_finish_run`` is sync. The actual
        fork uses ``asyncio.create_task`` inside
        ``background_review.fire_and_forget``; when no loop is running
        (e.g. tests calling _finish_run directly without an event
        loop), the call falls silent rather than raising.

        Subclasses that need additional behaviour can override this
        and call ``super()._after_response_delivered(...)`` to keep
        the Rip 1 wiring.
        """
        from robothor.engine import background_review

        background_review.fire_and_forget(session)

        # Rip 10: persist trajectory transcript (sampling controlled
        # by ROBOTHOR_TRAJECTORY_SAMPLE; 0.0 default → never).
        try:
            from robothor.engine.trajectory import save_trajectory_for_run

            save_trajectory_for_run(session, run)
        except Exception as exc:  # noqa: BLE001 — never block run finalization
            logger.debug("trajectory: post-response save raised: %s", exc)

        # PR-3a: evidence-based completion contracts. Flag-gated
        # off→observe→enforce (default off). When the run's final output
        # claims a session goal is done, verify the claim against recorded,
        # validated evidence rather than trusting the model's say-so.
        from robothor.engine.feature_flags import completion_contract_mode

        cc_mode = completion_contract_mode()
        if cc_mode != "off":
            try:
                from robothor.engine.completion_contract import check_completion_contract

                verdict = check_completion_contract(run, self.config)
            except Exception as exc:  # noqa: BLE001 — never block run finalization
                logger.debug("completion contract check raised: %s", exc)
                verdict = None
            if verdict is not None and verdict.status == "missing":
                reason = "; ".join(verdict.missing)
                try:
                    from robothor.engine.tracking import log_guardrail_event

                    log_guardrail_event(
                        run_id=run.id,
                        guardrail_name="completion_contract",
                        action="blocked" if cc_mode == "enforce" else "observed",
                        reason=reason,
                        mode=cc_mode,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("completion contract event log failed: %s", exc)
                if cc_mode == "alert":
                    # Middle rung: the claim stands, but the operator hears about it.
                    from robothor.engine.feature_flags import notify_guardrail_alert

                    notify_guardrail_alert(
                        guardrail_name="completion_contract",
                        agent_id=run.agent_id,
                        reason=reason,
                        tenant_id=getattr(run, "tenant_id", "") or "",
                    )
                if cc_mode == "enforce":
                    try:
                        from robothor.crm import dal

                        dal.set_next_action(
                            task_id=verdict.goal_id,
                            next_action=f"Provide evidence: {reason}"[:500],
                            agent=run.agent_id,
                            by="completion_contract",
                            tenant_id=run.tenant_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "completion contract enforce set_next_action failed: %s", exc
                        )

        # Deliverable contracts. The complement of the check above: that one
        # asks whether the agent's CLAIMS are backed by trace evidence, this
        # one asks whether the artifact the TASK named actually exists. A run
        # can pass the first and fail the second by doing the work correctly
        # and saving it somewhere else — measured 2026-08-26 as -0.87 of a
        # -1.04 competitive gap in which 7 of 10 tasks were at parity.
        from robothor.engine.feature_flags import deliverable_contract_mode

        dc_mode = deliverable_contract_mode()
        if dc_mode != "off":
            try:
                from robothor.engine.deliverable_contract import check_run_deliverables

                dreport = check_run_deliverables(run, session)
            except Exception as exc:  # noqa: BLE001 — never block finalization
                logger.debug("deliverable contract check raised: %s", exc)
                dreport = None
            # None means the task named no deliverable, which is most runs.
            # Logging a vacuous pass on every one of them would bury the real
            # verdicts in exactly the way the alert digest already does.
            if dreport is not None and not dreport.satisfied:
                try:
                    from robothor.engine.tracking import log_guardrail_event

                    log_guardrail_event(
                        run_id=run.id,
                        guardrail_name="deliverable_contract",
                        action="blocked" if dc_mode == "enforce" else "observed",
                        reason=dreport.message[:500],
                        mode=dc_mode,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("deliverable contract event log failed: %s", exc)
                if dc_mode in ("alert", "enforce"):
                    from robothor.engine.feature_flags import notify_guardrail_alert

                    notify_guardrail_alert(
                        guardrail_name="deliverable_contract",
                        agent_id=run.agent_id,
                        reason=dreport.message[:500],
                        tenant_id=getattr(run, "tenant_id", "") or "",
                    )

    @staticmethod
    def _check_primary_model_reached(run: AgentRun, agent_config: Any) -> None:
        """Alert when a run answered on a fallback instead of the configured primary.

        This is the single highest-leverage degradation detector: it turns an
        invisible, fleet-wide silent fallback (codex-not-on-PATH, 2026-05-29)
        into a per-run WARN + operator alert. Only fires for top-level runs that
        actually produced a model answer and whose used model differs from the
        manifest's primary.
        """
        if agent_config is None or getattr(run, "parent_run_id", None):
            return
        primary = getattr(agent_config, "model_primary", "") or ""
        used = run.model_used or ""
        if not primary or not used:
            return
        if _normalize_model_id(used) == _normalize_model_id(primary):
            return

        logger.error(
            "DEGRADED model: agent=%s ran on %s, not configured primary %s "
            "— primary_model_unreached=True",
            _sanitize(run.agent_id),
            _sanitize(used),
            _sanitize(primary),
        )
        note = f"Ran on fallback {used}, not primary {primary}"
        run.outcome_notes = f"{run.outcome_notes}; {note}" if run.outcome_notes else note

        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop()
            from robothor.engine.alerts import alert as _alert
            from robothor.engine.task_registry import get_task_registry

            get_task_registry().spawn(
                _alert(
                    "warning",
                    f"Primary model unreached: {run.agent_id}",
                    f"Agent `{run.agent_id}` completed on fallback `{used}` instead of "
                    f"its configured primary `{primary}`. The primary may be "
                    f"misconfigured or unavailable.",
                    metadata={
                        "agent_id": run.agent_id,
                        "model_used": used,
                        "model_primary": primary,
                    },
                ),
                name=f"primary-unreached-alert:{run.agent_id}",
            )

    @staticmethod
    def _publish_run_telemetry(trace: Any, run: AgentRun) -> None:
        """Emit run-level cache-hit-rate + token metrics (PR 4, observe-only).

        Computes the run's cumulative cache_read/cache_creation/prompt tokens
        and the derived cache_hit_ratio (cache_read / max(prompt_tokens, 1)),
        then:
        - emits them as GenAI semantic-convention attributes on a small
          ``run_summary`` span so they flow through the existing OTLP export
          (``TraceContext.build_otlp_payload`` serializes ``trace.spans``);
        - forwards the same numbers to ``trace.publish_metrics`` (Redis event
          bus), extending the run_data dict that already carries duration_ms/
          status/token counts — no new infra, no DB migration.

        Best-effort: telemetry must never break a completed run.
        """
        if not trace:
            return
        with contextlib.suppress(Exception):
            from robothor.engine.telemetry import cache_hit_ratio, gen_ai_attributes

            hit_ratio = cache_hit_ratio(run.cache_read_tokens, run.input_tokens)
            with trace.span(
                "run_summary",
                **gen_ai_attributes(
                    model=run.model_used or "",
                    input_tokens=run.input_tokens,
                    output_tokens=run.output_tokens,
                    cache_read_tokens=run.cache_read_tokens,
                    cache_creation_tokens=run.cache_creation_tokens,
                ),
            ):
                pass

            trace.publish_metrics(
                {
                    "status": "completed",
                    "duration_ms": run.duration_ms or 0,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "cache_creation_tokens": run.cache_creation_tokens,
                    "cache_read_tokens": run.cache_read_tokens,
                    "cache_hit_ratio": hit_ratio,
                }
            )

    def _finish_run(
        self,
        run: AgentRun,
        trace: Any = None,
        agent_config: Any = None,
        session: Any = None,
        spawn_context: Any = None,
    ) -> AgentRun:
        """Finalize run and spawn background DB persistence.

        Returns the AgentRun immediately — DB writes happen asynchronously
        so callers (especially interactive Telegram sessions) are not blocked.

        agent_config is optional — when passed, post-run guardrails
        (e.g. requires_human_task_closure) will run against the finished run.
        """
        # ── [GUARDRAIL] Post-run checks (require finished run + config) ──
        if agent_config is not None:
            try:
                from robothor.engine.guardrails import check_post_run

                check_post_run(run, agent_config, tenant_id=getattr(run, "tenant_id", ""))
            except Exception as e:
                logger.warning("post-run guardrail error: %s", _sanitize(e))

        # ── [OBSERVABILITY] Configured-primary-not-reached detector ──────
        # A run that completes on a fallback model looks identical to a healthy
        # run in the DB, so a dead primary (e.g. codex/gpt-5.5 missing from the
        # engine PATH) can degrade the whole fleet silently. Emit a loud signal
        # whenever the model that actually answered isn't the configured
        # primary. Sub-agent runs inherit the parent's models, so skip them.
        with contextlib.suppress(Exception):
            self._check_primary_model_reached(run, agent_config)

        # ── Phase 0 hook: post-response extension point ──────────────
        # No-op by default; future rips spawn background reviews
        # (Rip 1), persist trajectories (Rip 10), etc. Suppressed so a
        # broken hook never blocks the run from finalizing.
        if session is not None:
            with contextlib.suppress(Exception):
                self._after_response_delivered(session, run)

        # ── [STAGE 5] Lift unfinished todo_write items to the parent task ──
        # Closes the "full circle": a worker that ran out of
        # iterations/budget with pending items writes them back to the
        # CRM parent task so the thread planner picks up next beat.
        if os.environ.get("ROBOTHOR_TODO_ESCALATE_ENABLED", "1") == "1":
            try:
                todos = getattr(session, "todo_list", None) if session else None
                parent_task_id = spawn_context.parent_task_id if spawn_context else None
                if todos and parent_task_id:
                    esc_kwargs: dict[str, Any] = {
                        "todos": todos,
                        "parent_task_id": parent_task_id,
                        "agent_id": run.agent_id,
                        "tenant_id": getattr(run, "tenant_id", "") or "",
                        "agent_config": agent_config,
                        "run_id": getattr(run, "id", None),
                    }
                    # Escalation does up to ~15 blocking psycopg2 round-trips
                    # (get_task + set_next_action + update_task + per-item
                    # promotion). Offload to a worker thread so it never blocks
                    # the event loop — same spawn-or-run-sync pattern as
                    # _persist_run below. Sync fallback for CLI/tests (no loop).
                    try:
                        asyncio.get_running_loop()
                        from robothor.engine.task_registry import get_task_registry

                        get_task_registry().spawn(
                            self._escalate_unfinished_todos_bg(esc_kwargs),
                            name=f"todo-escalate:{run.id}",
                        )
                    except RuntimeError:
                        _escalate_unfinished_todos(**esc_kwargs)
            except Exception as e:
                logger.warning("todo escalation error: %s", _sanitize(e))

        # ── [RECENT ACTIONS] Cross-session surface for tracked agents ──
        try:
            from robothor.engine.recent_actions import record_run

            record_run(run, agent_config=agent_config)
        except Exception as e:
            logger.warning("recent_actions write error: %s", _sanitize(e))

        # ── [CONTACT 360] Timeline activity emission ──
        # Best-effort: no-op when the run is not linked to a person (cron jobs
        # and the like).  Runs in the caller thread; it's a single idempotent
        # INSERT, fast enough that backgrounding isn't worth the complexity.
        try:
            from robothor.engine.run_person_link import emit_run_timeline_activity

            emit_run_timeline_activity(run)
        except Exception as e:  # noqa: BLE001
            logger.warning("timeline_activity emit error: %s", _sanitize(e))

        # ── [HOOKS] AGENT_END lifecycle hook (fire-and-forget) ──
        try:
            from robothor.engine.hook_registry import HookContext, HookEvent, get_hook_registry

            hr = get_hook_registry()
            if hr:
                end_ctx = HookContext(
                    event=HookEvent.AGENT_END,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    output_text=run.output_text or "",
                    error=run.error_message or "",
                    metadata={
                        "status": run.status.value
                        if hasattr(run.status, "value")
                        else str(run.status),
                    },
                )
                try:
                    asyncio.get_running_loop()
                    from robothor.engine.task_registry import get_task_registry

                    get_task_registry().spawn(
                        hr.dispatch(HookEvent.AGENT_END, end_ctx),
                        name=f"agent-end-hook:{run.agent_id}",
                    )
                except RuntimeError:
                    pass
        except Exception as e:
            logger.warning("AGENT_END hook error: %s", _sanitize(e))

        # ── [VERIFICATION] Check the run's claims against its own tool trace ──
        # Must run BEFORE persistence: _assess_outcome executes inside
        # _persist_run_sync and the persisted agent_runs row carries the
        # verdict columns. Flag-gated and exception-suppressed — verification
        # is bookkeeping and must never break the agent's actual work.
        self._verify_run_claims(run)
        self._verify_metric_trends(run)

        # Spawn DB persistence as a background task so the caller gets the run back immediately.
        try:
            asyncio.get_running_loop()
            from robothor.engine.task_registry import get_task_registry

            get_task_registry().spawn(
                self._persist_run(run),
                name=f"persist-run:{run.id}",
            )
        except RuntimeError:
            # No event loop (CLI, tests) — persist synchronously
            self._persist_run_sync(run)

        return run

    def _verify_run_claims(self, run: AgentRun) -> None:
        """Stamp the run with a claim-verification verdict (flag-gated, never raises).

        An agent's own account of what it did is not evidence: production run
        ``6cb7e492-…`` reported "✅ Payment confirmed — $270 sent … via Venmo"
        on a trace consisting of one ``write_file`` to ``/tmp``, and no control
        noticed. ``run_verification`` compares the run's claims against the
        tools that actually succeeded.

        Ladder (``ROBOTHOR_RUN_VERIFICATION_ENABLED`` / ``_MODE``):
        ``observe`` records the verdict on the run and in
        ``agent_guardrail_events``; ``alert`` additionally notifies the
        operator; ``enforce`` records identically here. Nothing in THIS method
        mutates delivery, tasks or the outcome grade — acting on the verdict
        happens downstream in ``_update_task_for_run`` (task closure) and
        ``delivery._verification_banner`` (the operator-facing banner), which
        read the verdict this method stamped.
        """
        from robothor.engine.feature_flags import run_verification_mode

        mode = run_verification_mode()
        if mode == "off":
            return
        try:
            from robothor.engine.run_verification import verify_run

            verdict = verify_run(run.output_text, run.steps)
            run.verified_status = verdict.status
            run.verification = verdict.to_payload()
        except Exception as exc:  # noqa: BLE001 — never block run finalization
            logger.debug("run verification raised: %s", _sanitize(exc))
            return

        if verdict.status == "no_claims":
            return

        reason = verdict.summary()
        try:
            from robothor.engine.tracking import log_guardrail_event

            log_guardrail_event(
                run_id=run.id,
                guardrail_name="run_verification",
                action="allowed" if verdict.status == "verified" else "observed",
                reason=reason[:500],
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run verification event log failed: %s", _sanitize(exc))

        if mode in ("alert", "enforce") and verdict.status != "verified":
            # Middle rung made real: the claim stands, but the operator hears
            # about it (see feature_flags._enforcement_mode's ladder contract).
            with contextlib.suppress(Exception):
                from robothor.engine.feature_flags import notify_guardrail_alert

                notify_guardrail_alert(
                    guardrail_name="run_verification",
                    agent_id=run.agent_id,
                    reason=reason,
                    tenant_id=getattr(run, "tenant_id", "") or "",
                )

    def _verify_metric_trends(self, run: AgentRun) -> None:
        """Flag a published trend that contradicts this agent's own last figure.

        The 2026-08-22 morning briefing closed with ``Fleet health: 52.8%
        (↓0.5pp WoW)``. Its own previous briefing had published 48.6%, so the
        real change was +4.2pp and the delivered claim was a half-point fall.
        Neither the value nor the trend had a source — the run's tool output
        carried no fleet-health field and was truncated — and no control
        noticed, because the claim-verification spine checks claimed ACTIONS
        against the trace and says nothing about claimed NUMBERS.

        Deliberately narrow. A broader "is this number present in the run's tool
        outputs" check was built first and rejected on measurement: it missed
        this case and flagged 15 of 21 legitimate numbers on other days. This
        one needs no trace at all — only the agent's own words, twice — and
        replayed over 30 days of real briefings it fired once, on exactly the
        real defect, with no false positives.

        Silence is the default: no previous publication, or no claimed
        direction, means no finding.
        """
        from robothor.engine.feature_flags import run_verification_mode

        mode = run_verification_mode()
        if mode == "off" or not run.output_text:
            return
        try:
            from robothor.engine.stat_verification import (
                check_trend_consistency,
                extract_metric_claims,
            )

            claims = extract_metric_claims(run.output_text)
            if not any(c.delta is not None for c in claims):
                return
            previous = self._previous_delivered_output(run)
            violations = check_trend_consistency(claims, extract_metric_claims(previous))
        except Exception as exc:  # noqa: BLE001 — never block run finalization
            logger.debug("metric trend check raised: %s", _sanitize(exc))
            return

        if not violations:
            return

        reason = "; ".join(f"{v.label}: {v.detail}" for v in violations)
        logger.warning(
            "Agent %s published a trend contradicting its own last figure: %s",
            _sanitize(run.agent_id),
            _sanitize(reason),
        )
        try:
            from robothor.engine.tracking import log_guardrail_event

            log_guardrail_event(
                run_id=run.id,
                guardrail_name="metric_trend_consistency",
                action="observed",
                reason=reason[:500],
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001 — never block run finalization
            # Not suppressed: a guardrail event that fails to record leaves the
            # control firing with no trace, and the soak then reads "clean".
            logger.error(
                "metric trend guardrail event write FAILED (run=%s): %s",
                run.id,
                _sanitize(exc),
            )
        if mode in ("alert", "enforce"):
            with contextlib.suppress(Exception):
                from robothor.engine.feature_flags import notify_guardrail_alert

                notify_guardrail_alert(
                    guardrail_name="metric_trend_consistency",
                    agent_id=run.agent_id,
                    reason=reason,
                    tenant_id=getattr(run, "tenant_id", "") or "",
                )

    @staticmethod
    def _previous_delivered_output(run: AgentRun) -> str | None:
        """The last output this agent actually delivered before this run."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT output_text FROM agent_runs
                    WHERE agent_id = %s AND id <> %s
                      AND output_text IS NOT NULL
                      AND trigger_type = %s
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (
                        run.agent_id,
                        str(run.id),
                        getattr(run.trigger_type, "value", run.trigger_type),
                    ),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("previous-output lookup failed: %s", _sanitize(exc))
            return None

    async def _persist_run(self, run: AgentRun) -> None:
        """Persist run state and steps to the database in a background thread."""
        await asyncio.to_thread(self._persist_run_sync, run)

    async def _escalate_unfinished_todos_bg(self, kwargs: dict[str, Any]) -> None:
        """Run the blocking todo escalation/promotion off the event loop.

        Best-effort: the escalation is non-critical (the next heartbeat re-plans
        from the parent task anyway), so failures are logged, not raised.
        """
        try:
            await asyncio.to_thread(functools.partial(_escalate_unfinished_todos, **kwargs))
        except Exception as e:  # noqa: BLE001
            logger.warning("todo escalation (bg) error: %s", _sanitize(e))

    @staticmethod
    def _assess_outcome(run: AgentRun) -> None:
        """Assess run outcome using simple heuristics.

        All trigger types are assessed (cron, hook, workflow, interactive).
        Sub-agent runs are skipped (parent handles assessment).
        """
        from robothor.engine.models import RunStatus

        # Skip sub-agent runs
        if run.parent_run_id:
            return

        if run.status == RunStatus.TIMEOUT:
            run.outcome_assessment = "abandoned"
            run.outcome_notes = "Run timed out"
        elif run.status == RunStatus.FAILED:
            run.outcome_assessment = "incorrect"
            run.outcome_notes = f"Run failed: {(run.error_message or 'unknown')[:200]}"
        elif run.status == RunStatus.COMPLETED:
            if run.budget_exhausted:
                run.outcome_assessment = "partial"
                run.outcome_notes = "Budget exhausted before completion"
            elif run.error_message:
                run.outcome_assessment = "partial"
                run.outcome_notes = f"Completed with errors: {run.error_message[:200]}"
            elif not run.output_text or len(run.output_text.strip()) < 10:
                run.outcome_assessment = "partial"
                run.outcome_notes = "Completed with minimal output"
            elif (
                run.delivery_mode == DeliveryMode.ANNOUNCE
                and len(run.output_text.strip()) < ANNOUNCE_MIN_OUTPUT_CHARS
            ):
                run.outcome_assessment = "partial"
                run.outcome_notes = (
                    f"Thin announce output ({len(run.output_text.strip())} chars) "
                    "— likely meta-confirmation instead of full content"
                )
            else:
                run.outcome_assessment = "successful"
                run.outcome_notes = None

        # Claim verification is RECORDED here, never graded on: this note makes
        # an unverified run visible to anyone reading outcome_notes, but the
        # assessment itself is untouched so no existing consumer shifts under
        # the flag. Acting on the verdict belongs to the follow-up PR.
        RunFinalizationMixin._note_verification(run)

    @staticmethod
    def _note_verification(run: AgentRun) -> None:
        """Append the claim-verification verdict to ``outcome_notes``, if any."""
        status = getattr(run, "verified_status", None)
        if status not in ("unverified_claims", "failed_verification"):
            return
        payload = getattr(run, "verification", None) or {}
        kinds = payload.get("unsupported") or []
        label = "Unverified" if status == "unverified_claims" else "Failed verification for"
        note = f"{label} claims: {', '.join(str(k) for k in kinds) or 'unknown'}"
        run.outcome_notes = f"{run.outcome_notes} | {note}" if run.outcome_notes else note

    def _persist_run_sync(self, run: AgentRun) -> None:
        """Synchronous DB persistence — update run + batch-insert steps + CRM task."""
        # Assess outcome for interactive runs before persisting
        self._assess_outcome(run)

        try:
            update_run(
                run.id,
                status=run.status.value,
                completed_at=run.completed_at,
                duration_ms=run.duration_ms,
                model_used=run.model_used,
                models_attempted=run.models_attempted,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                cache_creation_tokens=run.cache_creation_tokens or None,
                cache_read_tokens=run.cache_read_tokens or None,
                total_cost_usd=run.total_cost_usd,
                output_text=run.output_text,
                error_message=run.error_message,
                error_traceback=run.error_traceback,
                delivery_status=run.delivery_status,
                delivered_at=run.delivered_at,
                delivery_channel=run.delivery_channel,
                token_budget=run.token_budget or None,
                cost_budget_usd=run.cost_budget_usd or None,
                budget_exhausted=run.budget_exhausted or None,
                outcome_assessment=run.outcome_assessment,
                outcome_notes=run.outcome_notes,
                verified_status=getattr(run, "verified_status", None),
                verification=getattr(run, "verification", None),
                # Re-assert task_id at run end: the write after auto-task
                # creation can fail or be lost to a crash mid-run. update_run
                # skips None fields, so taskless runs are unaffected.
                task_id=run.task_id,
            )
            # Flush only steps the session hasn't already committed —
            # per-iteration flushes (see AgentSession.flush_new_steps_sync)
            # persist along the way, so on normal completion this is
            # usually the tail (record_error + final assistant turn).
            # An untracked run has no agent_runs row, so every step insert
            # would fail the run_id FK — skip them entirely.
            pending = [] if run.tracking_disabled else run.steps[run.persisted_step_count :]
            if pending:
                try:
                    create_steps_batch(pending)
                except Exception:
                    for step in pending:
                        try:
                            create_step(step)
                        except Exception as e:
                            logger.warning("Failed to record step: %s", _sanitize(e))
                run.persisted_step_count = len(run.steps)
        except Exception as e:
            logger.warning("Failed to update run in database: %s", _sanitize(e))

        self._update_task_for_run(run)

    @staticmethod
    def _update_task_for_run(run: AgentRun) -> None:
        """Close, label or re-open the CRM task this run came from.

        THE DEFECT THIS GATES. A COMPLETED run used to close its originating
        task with ``f"Run completed: {output_text[:200]}"`` unconditionally:
        the agent's own claim became the permanent record and nothing checked
        it. 300 of the 571 tasks closed in the last 7 days on this box carry
        that string, and ``email-analyst`` holds 1,692 DONE tasks while having
        had no production run since 2026-06-14 — every one of those closures
        came from a benchmark run and carries benchmark fixture text as its
        resolution. "DONE" meant "an agent said something", not "work
        happened".

        Ladder (``ROBOTHOR_RUN_VERIFICATION_ENABLED`` / ``_MODE``):
          - ``off`` / ``observe``: byte-identical to the legacy behavior. The
            merge posture is observe, so merging this changes nothing.
          - ``alert``: the close still happens, but the resolution is labelled
            ``[verified]`` or ``[claimed]`` so the ledger distinguishes a shown
            completion from an asserted one. Task state is untouched.
          - ``enforce``: an ``unverified_claims`` / ``failed_verification``
            verdict does NOT close the task — a ``next_action`` naming the
            unsupported claims is written instead, leaving the task open and
            visible with a reason. Benchmark runs never close a production
            task at all, whatever the verdict.

        Never raises: task bookkeeping must not fail a finished run.
        """
        if not run.task_id:
            return
        try:
            from robothor.crm.dal import resolve_task as dal_resolve_task
            from robothor.crm.dal import update_task as dal_update_task
            from robothor.engine.feature_flags import run_verification_mode
            from robothor.engine.models import RunStatus

            if run.status in (RunStatus.FAILED, RunStatus.TIMEOUT):
                dal_update_task(
                    run.task_id,
                    status="TODO",
                    tags=[run.agent_id, "failed", run.status.value],
                )
                return
            if run.status != RunStatus.COMPLETED:
                return

            mode = run_verification_mode()
            if mode in ("off", "observe"):
                # The pin: nothing below runs until the flag is promoted.
                dal_resolve_task(
                    run.task_id,
                    resolution=f"Run completed: {(run.output_text or '')[:200]}",
                    agent_id=run.agent_id,
                )
                return

            from robothor.engine.analytics import is_benchmark_run
            from robothor.engine.run_verification import (
                blocks_resolution,
                next_action_for_unverified,
                resolution_prefix,
            )

            status = getattr(run, "verified_status", None)
            verification = getattr(run, "verification", None)

            if mode == "enforce" and is_benchmark_run(run.trigger_detail):
                logger.info(
                    "benchmark run %s left task %s open (benchmark work is not production work)",
                    _sanitize(run.id),
                    _sanitize(run.task_id),
                )
                return

            if mode == "enforce" and blocks_resolution(status):
                from robothor.constants import DEFAULT_TENANT
                from robothor.crm import dal

                dal.set_next_action(
                    task_id=run.task_id,
                    next_action=next_action_for_unverified(verification),
                    agent=run.agent_id,
                    by="run_verification",
                    tenant_id=getattr(run, "tenant_id", "") or DEFAULT_TENANT,
                )
                logger.info(
                    "run %s claimed work it cannot show — task %s stays open",
                    _sanitize(run.id),
                    _sanitize(run.task_id),
                )
                return

            prefix = resolution_prefix(status)
            dal_resolve_task(
                run.task_id,
                resolution=f"{prefix} Run completed: {(run.output_text or '')[:200]}",
                agent_id=run.agent_id,
            )
        except Exception as e:
            logger.warning("Auto-task update failed: %s", _sanitize(e))
