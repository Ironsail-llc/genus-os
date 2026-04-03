"""Permission escalation — lightweight human-in-the-loop for agent tool calls.

Most agents run fully autonomously. This module is opt-in only: when a
guardrail flags a tool call that needs human approval, it sends a Telegram
inline-keyboard prompt and waits for a response (denies on timeout).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ─── Data Model ─────────────────────────────────────────────────────


@dataclass
class EscalationRequest:
    """A pending permission escalation waiting for human approval."""

    request_id: str
    agent_id: str
    run_id: str
    tool_name: str
    tool_args: dict[str, Any]
    guardrail_name: str
    reason: str
    created_at: float
    result: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    approved: bool | None = None
    telegram_message_id: int | None = None


# ─── Manager ────────────────────────────────────────────────────────


class PermissionEscalationManager:
    """Manages human-in-the-loop approval prompts via Telegram.

    Agents that opt into permission escalation will pause execution until
    the human responds (or the timeout fires, which denies the call).
    """

    def __init__(self, *, bot: Any, chat_id: str) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._pending: dict[str, EscalationRequest] = {}
        # Keyed by "{agent_id}:{guardrail_name}" → set of tool names
        # approved for this run session.
        self._session_grants: dict[str, set[str]] = {}

    # ── Public API ──────────────────────────────────────────────────

    async def request_approval(
        self,
        agent_id: str,
        run_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        guardrail_name: str,
        reason: str,
        timeout_seconds: float = 300.0,
    ) -> bool:
        """Request human approval for a tool call.

        Returns True if approved, False if denied.  On timeout or delivery
        failure the call is **denied** (fail-secure).
        """
        # Fast path: session grant already given.
        grant_key = f"{agent_id}:{guardrail_name}"
        if grant_key in self._session_grants and tool_name in self._session_grants[grant_key]:
            logger.debug(
                "Session grant exists for %s tool=%s guardrail=%s — auto-approved",
                agent_id,
                tool_name,
                guardrail_name,
            )
            return True

        request = EscalationRequest(
            request_id=str(uuid.uuid4()),
            agent_id=agent_id,
            run_id=run_id,
            tool_name=tool_name,
            tool_args=tool_args,
            guardrail_name=guardrail_name,
            reason=reason,
            created_at=time.monotonic(),
        )
        self._pending[request.request_id] = request

        try:
            await self._send_prompt(request, timeout_seconds)
        except Exception:
            logger.exception("Failed to send escalation prompt for %s", request.request_id)
            # If we can't reach Telegram, deny — a tool flagged for human
            # review must not proceed without human confirmation.
            self._cleanup(request.request_id)
            return False

        try:
            await asyncio.wait_for(request.result.wait(), timeout=timeout_seconds)
        except TimeoutError:
            logger.warning(
                "Escalation %s timed out after %.0fs — denying (agent=%s tool=%s)",
                request.request_id,
                timeout_seconds,
                agent_id,
                tool_name,
            )
            request.approved = False
            self._cleanup(request.request_id)
            return False

        approved = bool(request.approved)
        self._cleanup(request.request_id)
        return approved

    def resolve(
        self,
        request_id: str,
        *,
        approved: bool,
        remember_session: bool = False,
    ) -> None:
        """Resolve a pending escalation request (called from callback handler)."""
        request = self._pending.get(request_id)
        if request is None:
            logger.warning("Attempted to resolve unknown escalation %s", request_id)
            return

        if request.result.is_set():
            logger.warning("Escalation %s already resolved; ignoring", request_id)
            return

        request.approved = approved

        if remember_session:
            grant_key = f"{request.agent_id}:{request.guardrail_name}"
            self._session_grants.setdefault(grant_key, set()).add(request.tool_name)
            logger.info(
                "Session grant saved: %s tool=%s",
                grant_key,
                request.tool_name,
            )

        # Wake up the waiting coroutine.
        request.result.set()

    def cleanup_expired(self, max_age: float = 600.0) -> int:
        """Remove stale requests older than *max_age* seconds.

        Returns the number of requests removed.
        """
        now = time.monotonic()
        expired = [rid for rid, req in self._pending.items() if (now - req.created_at) > max_age]
        for rid in expired:
            req = self._pending.pop(rid, None)
            if req is not None and not req.result.is_set():
                req.approved = False  # deny on expiry — fail-secure
                req.result.set()
                logger.warning("Expired stale escalation %s (agent=%s)", rid, req.agent_id)
        return len(expired)

    # ── Internal ────────────────────────────────────────────────────

    async def _send_prompt(
        self,
        request: EscalationRequest,
        timeout_seconds: float,
    ) -> None:
        """Build and send the Telegram approval prompt with inline keyboard."""
        # Lazy import — aiogram may not be installed in every environment.
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        args_summary = _brief_args(request.tool_args)

        text = (
            f"\U0001f510 Agent `{request.agent_id}` requesting approval\n"
            f"\n"
            f"Tool: `{request.tool_name}`\n"
            f"Args: {args_summary}\n"
            f"Policy: {request.guardrail_name}\n"
            f"Reason: {request.reason}\n"
            f"\n"
            f"Auto-denies in {int(timeout_seconds)}s if no response."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Approve",
                        callback_data=f"perm:approve:{request.request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Approve All",
                        callback_data=f"perm:all:{request.request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Deny",
                        callback_data=f"perm:deny:{request.request_id}",
                    ),
                ],
            ],
        )

        msg = await self._bot.send_message(
            self._chat_id,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        request.telegram_message_id = msg.message_id

    def _cleanup(self, request_id: str) -> None:
        """Remove a request from the pending map."""
        self._pending.pop(request_id, None)


# ─── Helpers ────────────────────────────────────────────────────────


def _brief_args(args: dict[str, Any], max_len: int = 200) -> str:
    """Return a compact, truncated representation of tool arguments."""
    try:
        raw = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    if len(raw) > max_len:
        return raw[: max_len - 3] + "..."
    return raw


# ─── Singleton ──────────────────────────────────────────────────────

_escalation_manager: PermissionEscalationManager | None = None


def get_permission_manager() -> PermissionEscalationManager | None:
    """Get the permission escalation manager singleton (or None if not initialised)."""
    return _escalation_manager


def init_permission_manager(bot: Any, chat_id: str) -> PermissionEscalationManager:
    """Initialise the permission escalation manager singleton."""
    global _escalation_manager
    _escalation_manager = PermissionEscalationManager(bot=bot, chat_id=chat_id)
    return _escalation_manager
