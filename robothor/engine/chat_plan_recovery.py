"""Restore only the saved pending plan belonging to a recovered execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.auth.deps import AuthContext
    from robothor.engine.chat import ChatSession


async def attach_plan(
    outcome: dict[str, Any],
    session: ChatSession | None,
    request_id: str,
    auth: AuthContext,
    session_key: str,
) -> None:
    import asyncio
    from dataclasses import fields

    from robothor.engine.chat import _get_session, _plan_is_expired, _plan_to_dict
    from robothor.engine.chat_store import load_plan_state
    from robothor.engine.models import PlanState

    # Run completion precedes plan persistence. Keep reading until the request's
    # delivery task finishes, so a fast poll cannot discard a still-saving draft.
    if (
        session is not None
        and session.active_request_id == request_id
        and session.active_task is not None
        and not session.active_task.done()
        and outcome.get("terminal")
    ):
        outcome["terminal"] = False
        outcome["delivery_pending"] = True
        return
    plan = session.active_plan if session is not None else None
    if (
        plan is None
        and (session is None or session.active_task is None)
        and outcome.get("plan_exploration")
        and outcome.get("terminal")
        and outcome.get("state") == "completed"
    ):
        saved = await asyncio.to_thread(load_plan_state, session_key, tenant_id=auth.tenant_id)
        if saved and saved.get("exploration_run_id") == outcome.get("run_id"):
            candidate = PlanState(
                plan_id=saved.get("plan_id", ""),
                plan_text=saved.get("plan_text", ""),
                original_message=saved.get("original_message", ""),
                created_at=saved.get("created_at", ""),
            )
            if saved.get("status") == "pending" and not _plan_is_expired(candidate):
                restored = _get_session(session_key)
                if restored.active_plan is None and restored.active_task is None:
                    names = {field.name for field in fields(PlanState)}
                    restored.active_plan = PlanState(
                        **{k: v for k, v in saved.items() if k in names}
                    )
                    outcome["plan"] = saved
        return
    if (
        outcome.get("state") == "completed"
        and plan is not None
        and plan.status == "pending"
        and not _plan_is_expired(plan)
        and str(plan.exploration_run_id) == outcome.get("run_id")
    ):
        outcome["plan"] = _plan_to_dict(plan)
