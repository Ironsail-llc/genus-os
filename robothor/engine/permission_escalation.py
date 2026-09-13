"""Permission escalation — lightweight human-in-the-loop for agent tool calls.

Most agents run fully autonomously. This module is opt-in only: when a guardrail
flags a tool call that needs human approval, it asks a person and waits for a
response, denying on timeout.

Two surfaces, and the order between them matters
------------------------------------------------
The **bot** path is the one an operator sees today: a Telegram inline keyboard
whose ``callback_data`` is ``perm:approve|all|deny:<request_id>``, resolved by
``TelegramHandlersMixin.on_permission_decision``. It is kept byte-for-byte
because it is production UX and because a live prompt in a chat outlives a
deploy — change the callback data and every prompt sent before the restart
becomes a button that does nothing.

The **channel** path is :meth:`Channel.ask`, and it is what makes this manager
work on a surface that is not Telegram. It is also the fallback when the bot
cannot deliver: before it existed, an undeliverable prompt was an instant
denial, which is right only if there was genuinely nobody else to ask.

Either way the answer comes from a person or it does not come at all. A timeout
denies, an unreachable surface denies, and an answer nobody recognises denies.
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

#: The three answers an escalation accepts, in the order they are offered. The
#: button labels on the Telegram keyboard are these same strings, so a channel
#: ask and a keyboard tap are the same three choices worded identically — an
#: operator who has learned one surface has learned both.
APPROVE = "Approve"
APPROVE_ALL = "Approve All"
DENY = "Deny"
ESCALATION_OPTIONS = (APPROVE, APPROVE_ALL, DENY)


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
    #: The monotonic instant this request's OWN budget runs out —
    #: ``created_at + timeout_seconds``, where ``timeout_seconds`` is the
    #: agent's ``human_approval_timeout`` (validated 10..3600). ``None`` for a
    #: request built by hand rather than by :meth:`request_approval`, which
    #: keeps the legacy ``max_age``-only rule for those.
    #:
    #: This exists because the watchdog sweep was reaping on a flat 600 s and
    #: force-denying agents configured for longer, with their keyboard still
    #: live. A control may not be stricter than the budget it enforces.
    expires_at: float | None = None
    result: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    approved: bool | None = None
    telegram_message_id: int | None = None


# ─── Manager ────────────────────────────────────────────────────────


class PermissionEscalationManager:
    """Manages human-in-the-loop approval prompts via Telegram.

    Agents that opt into permission escalation will pause execution until
    the human responds (or the timeout fires, which denies the call).
    """

    def __init__(
        self,
        *,
        bot: Any = None,
        chat_id: str = "",
        channel: Any = None,
        target: str = "",
    ) -> None:
        """Build a manager over a Telegram bot, a channel, or both.

        Both is the production wiring, and the bot wins: see the module
        docstring for why the keyboard path is not merely preferred but
        preserved. ``channel``/``target`` are what a non-Telegram deployment
        runs on, and what the bot path falls through to when it cannot deliver.
        """
        self._bot = bot
        self._chat_id = chat_id
        self._channel = channel
        self._target = target
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
            expires_at=time.monotonic() + max(1.0, float(timeout_seconds)),
        )
        self._pending[request.request_id] = request

        # Tell the run's own surface that it is now waiting on a person. In
        # RAM by design: this is a notification, and the pending request is
        # what the answer is resolved against.
        await self._announce(request, timeout_seconds)

        prompted = False
        if self._bot is not None:
            try:
                await self._send_prompt(request, timeout_seconds)
                prompted = True
            except Exception:
                logger.exception("Failed to send escalation prompt for %s", request.request_id)

        if not prompted:
            # Either there is no bot at all, or the bot could not deliver.
            # Before falling back to a denial, try the other surface — "the
            # operator could not be reached" is a claim worth testing before
            # it is acted on.
            approved = await self._ask_over_channel(request, timeout_seconds)
            self._cleanup(request.request_id)
            return approved

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
    ) -> bool:
        """Resolve a pending escalation. True only if THIS call settled it.

        The return value is what lets the bridge's answer endpoint tell "the
        operator just decided" from "that prompt is long gone" — answering 200
        to a tap that changed nothing is how a UI ends up showing an approval
        that never reached the agent. The Telegram callback handler ignores it
        and is unaffected.
        """
        request = self._pending.get(request_id)
        if request is None:
            logger.warning("Attempted to resolve unknown escalation %s", request_id)
            return False

        if request.result.is_set():
            logger.warning("Escalation %s already resolved; ignoring", request_id)
            return False

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
        return True

    def cleanup_expired(self, max_age: float = 600.0) -> int:
        """Reap orphaned requests. Returns how many were removed.

        ``max_age`` is a **floor, never a ceiling**. A request is reaped only
        once it is older than ``max_age`` AND past its own ``expires_at`` — so
        this sweep can be late, and can never be early. That asymmetry is the
        whole point: ``human_approval_timeout`` is per-agent and validated to
        10..3600, the runbook promises the call auto-denies after *that*, and a
        housekeeping sweep that denied at 600 s while the operator's keyboard
        was still live would be a control out-voting the budget it exists to
        enforce. Being late costs nothing — the waiting coroutine denies itself
        on its own ``wait_for``, so anything this reaps is already an orphan
        (a cancelled task, a killed run) with nobody listening.

        A request built by hand carries no ``expires_at`` and keeps the legacy
        ``max_age``-only rule.
        """
        now = time.monotonic()
        expired = [
            rid
            for rid, req in self._pending.items()
            if (now - req.created_at) > max_age
            and (req.expires_at is None or now >= req.expires_at)
        ]
        for rid in expired:
            req = self._pending.pop(rid, None)
            if req is not None and not req.result.is_set():
                req.approved = False  # deny on expiry — fail-secure
                req.result.set()
                logger.warning("Expired stale escalation %s (agent=%s)", rid, req.agent_id)
        return len(expired)

    # ── Internal ────────────────────────────────────────────────────

    async def _announce(self, request: EscalationRequest, timeout_seconds: float) -> None:
        """Emit ``approval_required`` to whatever is watching this run.

        Emitted here rather than at the escalate branch in ``tool_admission``
        for one concrete reason: the id. A waiting web client needs the
        ``request_id`` to answer the prompt through the bridge, and that id does
        not exist until this method's caller has built the request. Emitting one
        frame earlier would mean announcing a question nobody could reply to.
        """
        from robothor.engine.run_status import emit_status

        await emit_status(
            request.run_id,
            {
                "event": "approval_required",
                "kind": "escalation",
                "id": request.request_id,
                "run_id": request.run_id,
                "agent_id": request.agent_id,
                "tool": request.tool_name,
                "question": _escalation_question(request),
                "options": list(ESCALATION_OPTIONS),
                "timeout_seconds": timeout_seconds,
            },
        )

    def _resolve_channel(self) -> Any | None:
        """The surface to ask on, resolved late and cached.

        Late on purpose. ``daemon.main`` wires this manager while it is building
        the bot, ~70 lines before ``_start_channels`` calls ``warm_channels()``
        — so a channel captured at construction time would be whatever the
        registry held before warm-up, forever. The resolution that matters
        happens at the moment an escalation is actually raised, which is always
        after startup finished.

        A manager built with an explicit ``channel`` keeps it. One built around
        the Telegram bot falls back to the Telegram *channel*, which is the same
        operator on the same surface reached a different way — the point being
        that an undeliverable keyboard is not by itself proof that nobody could
        be reached.
        """
        if self._channel is None and self._bot is not None:
            from robothor.engine.channels import get_channel

            self._channel = get_channel("telegram")
            self._target = self._target or self._chat_id
        return self._channel

    async def _ask_over_channel(self, request: EscalationRequest, timeout_seconds: float) -> bool:
        """Ask through :meth:`Channel.ask`. Every way of not knowing is a deny.

        ``None`` (nobody answered), ``NotImplementedError`` (a surface with
        nobody to ask), a broken transport, and an answer that is not one of the
        three offered all mean the same thing here. A guardrail escalation that
        proceeded on any of them would be an approval nobody gave.
        """
        channel = self._resolve_channel()
        if channel is None:
            logger.warning("Escalation %s has no surface to ask on — denying", request.request_id)
            return False

        try:
            answer = await channel.ask(
                _escalation_question(request, timeout_seconds),
                ESCALATION_OPTIONS,
                timeout=timeout_seconds,
                target=self._target,
            )
        except NotImplementedError:
            logger.warning(
                "Channel %s cannot ask; denying escalation %s",
                getattr(channel, "name", "?"),
                request.request_id,
            )
            return False
        except Exception:
            logger.exception("Channel ask failed for escalation %s", request.request_id)
            return False

        if answer == APPROVE_ALL:
            grant_key = f"{request.agent_id}:{request.guardrail_name}"
            self._session_grants.setdefault(grant_key, set()).add(request.tool_name)
            logger.info("Session grant saved: %s tool=%s", grant_key, request.tool_name)
            return True
        if answer == APPROVE:
            return True
        if answer is not None and answer != DENY:
            logger.warning(
                "Escalation %s got an answer that is not one of the options (%r) — denying",
                request.request_id,
                answer,
            )
        return False

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
                        text=APPROVE,
                        callback_data=f"perm:approve:{request.request_id}",
                    ),
                    InlineKeyboardButton(
                        text=APPROVE_ALL,
                        callback_data=f"perm:all:{request.request_id}",
                    ),
                    InlineKeyboardButton(
                        text=DENY,
                        callback_data=f"perm:deny:{request.request_id}",
                    ),
                ],
            ],
        )

        # ``self._bot`` is normally the raw aiogram ``Bot`` — but daemon.py
        # wires the higher-level ``TelegramBot`` wrapper. That wrapper's own
        # ``send_message`` is a chunking/markdown-conversion helper meant for
        # plain chat delivery: it silently drops unknown kwargs (including
        # ``reply_markup``/``parse_mode``) and returns a ``list[Message]``
        # instead of a single message. Sending through it would deliver a
        # prompt with no inline keyboard at all and then blow up on
        # ``msg.message_id`` below. Reach for the wrapper's own raw bot so
        # the keyboard is actually attached and we get one Message back.
        from robothor.engine.telegram import TelegramBot

        sender = self._bot.bot if isinstance(self._bot, TelegramBot) else self._bot

        msg = await sender.send_message(
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


def _escalation_question(request: EscalationRequest, timeout_seconds: float | None = None) -> str:
    """The prompt text, shared by both surfaces so they read the same.

    The Telegram keyboard's own body is built in :meth:`_send_prompt` and keeps
    its Markdown; this is the plain rendering a channel gets, and the one the
    ``approval_required`` status event carries to a web client.
    """
    text = (
        f"Agent {request.agent_id} is requesting approval.\n"
        f"Tool: {request.tool_name}\n"
        f"Args: {_brief_args(request.tool_args)}\n"
        f"Policy: {request.guardrail_name}\n"
        f"Reason: {request.reason}"
    )
    if timeout_seconds is not None:
        text += f"\n\nAuto-denies in {int(timeout_seconds)}s if no response."
    return text


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


def init_permission_manager(
    bot: Any,
    chat_id: str,
    *,
    channel: Any = None,
    target: str = "",
) -> PermissionEscalationManager:
    """Initialise the permission escalation manager singleton.

    ``bot``/``chat_id`` stay positional because the daemon and the existing
    tests call it that way. ``channel``/``target`` arm the second surface — see
    :class:`PermissionEscalationManager` for which one is asked first.
    """
    global _escalation_manager
    _escalation_manager = PermissionEscalationManager(
        bot=bot, chat_id=chat_id, channel=channel, target=target or chat_id
    )
    return _escalation_manager


def fail_closed_on_missing_manager() -> bool:
    """Whether to DENY a human-approval escalation when no approver is reachable.

    True only in enforce mode (ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED=1 +
    ROBOTHOR_APPROVAL_MODE=enforce). Otherwise the legacy behavior of
    auto-approving an un-answerable escalation is preserved.
    """
    from robothor.engine.feature_flags import approval_mode

    return approval_mode() == "enforce"
