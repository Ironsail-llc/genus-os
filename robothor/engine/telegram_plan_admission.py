"""Validate Telegram plan ownership and content before background execution."""

from __future__ import annotations

from typing import Any

from robothor.engine.plan_integrity import plan_hash


def approval_error(bot: Any, callback: Any, plan: Any) -> str | None:
    if plan.status != "pending":
        return "Plan is already being executed"
    if plan.plan_hash and plan.plan_hash != plan_hash(plan.plan_text):
        return "Plan changed; request a new revision"
    creator = plan.creator_sender_info or {}
    sender_id = str(callback.from_user.id) if callback.from_user else ""
    if sender_id != str(creator.get("telegram_user_id", "")) and not bot._sender_is_owner(
        sender_id
    ):
        return "Only the plan author or instance owner can approve"
    return None
