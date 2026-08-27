"""Keep the message list inside budget before each LLM call.

Two steps that share one job — thin what is already there, then compact if the
estimate is still over — extracted from `_run_loop` as part of decomposing the
god-object the competitive analysis puts first.

Three properties are load-bearing and were asserted nowhere while this lived
inline in a 963-line method:

* it sizes against the model that will ACTUALLY be tried next, not the
  configured primary (G2b). A run on a smaller-window fallback compacting at
  the primary's larger threshold can overflow the fallback outright.
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
from robothor.engine.run_budget import proactive_compaction_threshold
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
        from robothor.engine.context import estimate_tokens, maybe_compress
        from robothor.engine.model_registry import get_model_limits

        est_tokens = estimate_tokens(session.messages)
        model_limits = get_model_limits(LLMClient.sizing_model(models, broken_models))
        threshold = proactive_compaction_threshold(model_limits.max_input_tokens)
        if est_tokens <= threshold:
            return

        pre_len = len(session.messages)
        await _dispatch(
            hook_registry,
            "PRE_COMPACTION",
            agent_config,
            session,
            {"est_tokens": est_tokens, "threshold": threshold, "message_count": pre_len},
        )

        session.messages[:] = await maybe_compress(session.messages, models, threshold=threshold)
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
