"""Bind native business dispatch to the durable effect ledger.

Calendar attendee operations retain their existing provider-specific ledger.
The deferred tool wrapper is accounted at its actual underlying dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from robothor.engine.runtime import effects
from robothor.engine.runtime.current import active_context
from robothor.engine.tools.constants import READONLY_TOOLS
from robothor.goals.runtime import RECOVERY_BOOKKEEPING_ACTIONS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.runtime.contracts import ExecutionContext
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)


def _bypass(name: str, args: dict[str, Any], ctx: ToolContext) -> bool:
    return (
        ctx.is_benchmark
        or name in READONLY_TOOLS
        or name in {"tool_call", "gws_calendar_add_attendees"}
        or name == "update_pursuit_goal"
        and args.get("action") in RECOVERY_BOOKKEEPING_ACTIONS
    )


def _unknown(identifier: Any, message: str) -> dict[str, Any]:
    return {
        "error": message,
        "outcome_unknown": True,
        "retryable": False,
        "effect_id": str(identifier),
    }


async def invoke(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    dispatch: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    context = active_context.get()
    if context is None or _bypass(name, args, ctx):
        return await dispatch()
    if context.tenant_id != ctx.tenant_id or not ctx.run_id or not ctx.agent_id:
        return {
            "error": "Trusted runtime identity is required for effect admission",
            "retryable": False,
        }
    # Native admission resolves delegated/restored identity after the outer runtime
    # context is constructed. Use the executor's authenticated business principal.
    context = replace(context, principal_id=ctx.user_id or context.principal_id)
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.read_only import declared_read_only_tools

    # Core reads already bypassed above. Extensions cannot own or reclassify a
    # core write, so avoid rediscovering plugins for those authoritative names.
    if name not in builtin_handlers() and name in await asyncio.to_thread(declared_read_only_tools):
        return await dispatch()
    try:
        record = await _reserve(context, name, args, ctx)
    except effects.EffectPendingError as exc:
        return _unknown(exc.effect_id, str(exc))
    except Exception:
        logger.warning("Effect admission unavailable; dispatch refused", exc_info=True)
        return {
            "error": "Could not record action admission; no action was dispatched",
            "retryable": False,
        }
    if record["state"] == "confirmed":
        if name == "create_task":
            from robothor.engine.runtime.task_report import publish

            await publish(context, record["id"], args, ctx, replayed=True)
        return {**record["resolution"]["result"], "effect_id": str(record["id"]), "recovered": True}
    if record["state"] == "finished":
        return {
            **record["resolution"]["result"],
            "effect_id": str(record["id"]),
            "recovered": True,
            "verification": "reported",
        }
    result = await _dispatch_reserved(context, record, name, args, ctx, dispatch)
    if name == "create_task":
        from robothor.engine.runtime.task_report import publish

        await publish(context, record["id"], args, ctx)
    return result


def _begin_outcome(*args: Any) -> dict[str, Any] | effects.EffectPendingError:
    try:
        return effects.begin(*args)
    except effects.EffectPendingError as exc:
        return exc


async def _reserve(
    context: ExecutionContext, name: str, args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    from robothor.engine.task_registry import get_task_registry

    registry = get_task_registry()
    pending = registry.spawn(
        asyncio.to_thread(_begin_outcome, context, ctx.run_id, ctx.agent_id, name, args),
        name="effect-admission",
    )
    try:
        # The task registry hands back an untyped task, so state what
        # `_begin_outcome` actually returns rather than let Any leak out.
        result: dict[str, Any] | effects.EffectPendingError = await asyncio.shield(pending)
        if isinstance(result, effects.EffectPendingError):
            raise result
        return result
    except asyncio.CancelledError:
        registry.spawn(
            _withdraw_pending(pending, context, ctx.run_id), name="effect-admission-cleanup"
        )
        raise


async def _withdraw_pending(
    pending: Awaitable[dict[str, Any] | effects.EffectPendingError],
    context: ExecutionContext,
    run_id: str,
) -> None:
    record = await pending
    if isinstance(record, effects.EffectPendingError):
        return
    if record["state"] == "prepared":
        await asyncio.to_thread(effects.finish, context, record["id"], run_id, uncertain=False)


async def _dispatch_reserved(
    context: ExecutionContext,
    record: dict[str, Any],
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    dispatch: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import _runtime_denial

    started = False
    try:
        # Reservation may wait. Recheck stop, lease and deadline before dispatch.
        denial = await _runtime_denial(name, args, ctx)
        if denial:
            await asyncio.to_thread(
                effects.finish, context, record["id"], ctx.run_id, uncertain=False
            )
            return denial
        if not await asyncio.to_thread(effects.mark_dispatched, context, record["id"], ctx.run_id):
            return {
                "error": "Effect admission was withdrawn; no action was dispatched",
                "retryable": False,
            }
        started = True
        token = effects.active_effect.set(record)
        try:
            result = await dispatch()
        finally:
            effects.active_effect.reset(token)
    except BaseException:
        # Before invoking the handler we can prove no effect was dispatched.
        # Afterwards cancellation does not prove the provider cancelled its work.
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    effects.finish, context, record["id"], ctx.run_id, uncertain=started
                )
            )
        except Exception:
            logger.warning("Interrupted effect record requires recovery", exc_info=True)
        raise
    uncertain = isinstance(result, dict) and (
        result.get("outcome_unknown") is True or result.get("tool_crashed") is True
    )
    verify_success = (
        record.get("_native_readback") is True
        and isinstance(result, dict)
        and not result.get("error")
    )
    try:
        from robothor.engine.runtime import effect_results

        if not uncertain and not verify_success and effect_results.cacheable(result):
            recorded = await asyncio.to_thread(
                effect_results.record, context, record["id"], ctx.run_id, result
            )
        else:
            recorded = await asyncio.to_thread(
                effects.finish,
                context,
                record["id"],
                ctx.run_id,
                uncertain=uncertain or verify_success,
            )
        if not recorded:
            return _unknown(
                record["id"],
                "Action ownership changed; its recorded outcome requires reconciliation",
            )
    except Exception:
        logger.warning("Effect result persistence failed", exc_info=True)
        return _unknown(
            record["id"], "The action was dispatched, but its outcome could not be recorded"
        )
    if uncertain or verify_success:
        from robothor.engine.runtime import note_recovery, task_recovery

        adapter = task_recovery if name == "create_task" else note_recovery
        recovered = await asyncio.to_thread(adapter.recover, context, record["id"])
        if recovered is not None:
            if verify_success and not uncertain:
                # First-call acknowledgement plus host readback is verification,
                # not recovery of an interrupted or previously recorded action.
                return {**result, **recovered, "recovered": False}
            return {**result, **recovered} if verify_success else recovered
        return {
            **result,
            **_unknown(record["id"], result.get("error") or "Action outcome is unknown"),
        }
    return result
