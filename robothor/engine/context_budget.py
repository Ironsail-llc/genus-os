"""Keep the message list inside budget before each LLM call.

Two steps that share one job — thin what is already there, then compact if the
estimate is still over — extracted from `_run_loop` as part of decomposing the
god-object the competitive analysis puts first.

Three properties are load-bearing and were asserted nowhere while this lived
inline in a 963-line method:

* it sizes against the model that will ACTUALLY be tried next, not the
  configured primary (G2b). A run on a smaller-window fallback compacting at
  the primary's larger threshold can overflow the fallback outright. "Tried
  next" means REACHABLE (`context_fit.next_reachable_model`): a model skipped
  because its credential pool is spent is not "broken", and for a day in
  September 2026 that distinction sized every call of a local-tier run against
  a primary it had never once reached.
* the ceiling behind the threshold is enforced whether or not compaction
  worked. Compaction summarises with a MODEL, so the run that most needs the
  ceiling is the one whose summariser cannot reach one either.
* it runs EVERY iteration. It used to run every fifth, and at ~10K tokens an
  iteration a five-gap overshoots the budget by half the budget again before
  anything looks. `estimate_tokens` is a cheap length sum.
* it never raises. Losing compaction costs money; taking the run down with it
  costs the work.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from robothor.engine.llm_client import LLMClient
from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)


async def keep_context_within_budget(
    session: Any,
    agent_config: Any,
    *,
    iteration: int,
    models: list[str],
    broken_models: set[str],
    hook_registry: Any,
    pre_iteration_msg_idx: int,
) -> None:
    """Thin, then compact, in place. Never raises.

    The first iteration has nothing behind it to thin and nothing accumulated
    to compact, so both steps are skipped.
    """
    if iteration <= 0:
        return
    _thin(session, agent_config, pre_iteration_msg_idx)
    await _compact(session, agent_config, iteration, models, broken_models, hook_registry)


def _thin(session: Any, agent_config: Any, pre_iteration_msg_idx: int) -> None:
    """Shrink earlier iterations' tool results, protecting this iteration's.

    Guarded separately from compaction: a thinning error used to be able to
    skip the compaction behind it, which is the expensive half.
    """
    if not getattr(agent_config, "eager_tool_compression", False):
        return
    try:
        chars_saved = session.thin_previous_tool_results(
            protect_after_index=pre_iteration_msg_idx,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Eager tool compression failed: %s", _sanitize(e))
        return
    if chars_saved > 0:
        logger.debug("Eager tool compression saved ~%d tokens", chars_saved // 4)


async def _compact(
    session: Any,
    agent_config: Any,
    iteration: int,
    models: list[str],
    broken_models: set[str],
    hook_registry: Any,
) -> None:
    try:
        from robothor.engine.context_fit import estimate_for, fit_for

        fit = fit_for(LLMClient.sizing_model(models, broken_models))
        # Sized with the ceiling's own estimator, not the flat heuristic: this
        # decides whether a conversation of CSV or base64 gets compacted at
        # all, and `chars / 4` is 0.28x of the truth on that content.
        est_tokens = estimate_for(session.messages, fit.model)
    except Exception as e:  # noqa: BLE001 — no budget means no enforcement either
        logger.warning("Context budget could not be computed: %s", _sanitize(e))
        return
    if est_tokens <= fit.threshold:
        return
    await _compact_and_enforce(
        session, agent_config, iteration, models, fit, hook_registry, broken_models
    )


async def _compact_and_enforce(
    session: Any,
    agent_config: Any,
    iteration: int,
    models: list[str],
    fit: Any,
    hook_registry: Any,
    broken_models: set[str] | None = None,
) -> None:
    """Summarise if a model can, then enforce the ceiling whether it could or not.

    Two separate guards on purpose. Compaction calls a MODEL and the run that
    needs the ceiling most is the one where no model answers — on 2026-09-16
    the summariser would have walked the same dead chain, and a compaction
    failure that also skipped the ceiling is how oversized messages reached a
    server that truncates them in silence.
    """
    from robothor.engine.context import maybe_compress
    from robothor.engine.context_fit import estimate_for

    threshold = fit.threshold
    try:
        est_tokens = estimate_for(session.messages, fit.model)
        pre_len = len(session.messages)
        await _dispatch(
            hook_registry,
            "PRE_COMPACTION",
            agent_config,
            session,
            {"est_tokens": est_tokens, "threshold": threshold, "message_count": pre_len},
        )

        session.messages[:] = await maybe_compress(
            session.messages, models, threshold=threshold, broken_models=broken_models
        )
        logger.info(
            "Proactive compaction at iter %d: %d→%d messages (est %d tokens, threshold %d)",
            iteration,
            pre_len,
            len(session.messages),
            est_tokens,
            threshold,
        )

        await _dispatch(
            hook_registry,
            "POST_COMPACTION",
            agent_config,
            session,
            {"pre_message_count": pre_len, "post_message_count": len(session.messages)},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Proactive compaction failed: %s", _sanitize(e))
    try:
        enforce_hard_limit(session, fit)
    except Exception as e:  # noqa: BLE001 — never take the run down for this
        logger.warning("Context ceiling could not be enforced: %s", _sanitize(e))


def enforce_hard_limit(session: Any, fit: Any) -> bool:
    """Drop what still does not fit, and tell the agent that it did.

    Compaction summarises with a MODEL, and the run that needs this most is
    the one where every cloud model is unreachable — the summariser walks its
    own chain and can come back having changed nothing. So the ceiling is
    enforced deterministically afterwards, or the messages go to a server that
    truncates them in silence and answers with a structural error.

    Returns whether the run was given a note — which is also the case when the
    shrink could NOT reach the ceiling, because an agent whose conversation is
    about to be truncated by the server needs telling either way. The
    token-comparison test belongs to `shrink_after_overflow`, whose question is
    different: may this call spend its one retry? Never raises.
    """
    from robothor.engine.context_fit import enforce_ceiling

    return enforce_ceiling(session.messages, fit)


async def _dispatch(
    hook_registry: Any, event_name: str, agent_config: Any, session: Any, metadata: dict[str, Any]
) -> None:
    """Best-effort hook dispatch.

    Suppressed individually so a third-party hook cannot disable compaction
    fleet-wide by raising.
    """
    if not hook_registry:
        return
    from robothor.engine.hook_registry import HookContext, HookEvent

    event = getattr(HookEvent, event_name)
    with contextlib.suppress(Exception):
        await hook_registry.dispatch(
            event,
            HookContext(
                event=event,
                agent_id=agent_config.id,
                run_id=session.run_id,
                metadata=metadata,
            ),
        )
