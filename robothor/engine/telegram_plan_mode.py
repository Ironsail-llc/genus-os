"""Plan-mode and deep-mode flows for the Telegram delivery layer.

Extracted from telegram.py 2026-08-24 (phase 3 of the god-object
decomposition; telegram.py was 3,792 lines — the delivery layer's twin of the
runner disease). This cluster is the interactive planning surface: propose a
plan, iterate it with the operator, execute an approved plan, and the deep
(RLM) variants.

CONTRACT — the allowed composed surface, type-enforced by the TYPE_CHECKING
stubs on the class: the bot/config/runner handles, the chat bookkeeping dicts,
and the named TelegramBot helpers below. A new ``self.*`` dependency outside
the stubs is a mypy error: put it on the signature instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram.enums import ChatAction

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

from robothor.engine.chat import (
    _extract_plan_text,
    _plan_is_expired,
)
from robothor.engine.chat_store import (
    clear_plan_state_async,
    save_exchange_async,
    save_plan_state_async,
)
from robothor.engine.models import PlanState, TriggerType
from robothor.engine.task_registry import get_task_registry

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner


logger = logging.getLogger(__name__)


TYPING_INTERVAL = 4  # seconds between typing indicator refreshes


def _plan_state_to_dict(plan: PlanState) -> dict[str, Any]:
    """Serialize PlanState to dict for DB persistence."""
    return {
        "plan_id": plan.plan_id,
        "plan_text": plan.plan_text,
        "original_message": plan.original_message,
        "status": plan.status,
        "created_at": plan.created_at,
        "exploration_run_id": plan.exploration_run_id,
        "rejection_feedback": plan.rejection_feedback,
        "revision_count": plan.revision_count,
        "revision_history": plan.revision_history,
        "execution_run_id": plan.execution_run_id,
        "deep_plan": plan.deep_plan,
        "creator_sender_info": plan.creator_sender_info,
    }


# Extensions we'll try to read as text


class PlanModeMixin:
    """See module docstring for the contract."""

    if TYPE_CHECKING:
        from robothor.engine.config import EngineConfig
        from robothor.engine.runner import AgentRunner

        bot: Bot
        config: EngineConfig
        runner: AgentRunner
        _active_tasks: dict[str, Any]
        _chat_user_info: dict[str, Any]
        _max_history: int
        _model_override: dict[str, str]

        def _build_background_config(self) -> Any: ...

        def _build_identity(self, *a: Any, **k: Any) -> Any: ...

        def _build_plan_keyboard(self, plan_id: str, revision_count: int = 0) -> Any: ...

        def _get_tenant_id(self, chat_id: str) -> str: ...

        async def _record_interactive_delivery(self, run: Any, sent: Any) -> None: ...

        def _resolve_user(self, chat_id: str, message: Any) -> dict[str, Any] | None: ...

        async def send_message(self, chat_id: str, text: str, **_ignored: Any) -> list[Any]: ...

    async def _run_plan_mode(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        user_text: str,
        message: Message,
        deep_plan: bool = False,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Execute agent in plan mode with read-only tools, display plan with approval keyboard.

        ``sender_info`` (Task 4 Finding 1 fix) is the per-message resolved
        sender identity, threaded exactly like ``_run_interactive``'s
        ``sender_info`` parameter. When the caller (``handle_text``/
        ``handle_file``) already resolved it, it's passed straight through.
        When omitted (``cmd_plan``/``cmd_deep`` invoke this directly with no
        prior resolution), this resolves fresh from ``message`` here — still
        a per-message resolution, keyed to THIS message's actual sender via
        ``message.from_user.id`` — never the shared, chat_id-keyed
        ``_chat_user_info`` cache, which reflects whichever sender happened
        to message last and can be stale or, in a group chat with concurrent
        senders, outright wrong for the message actually driving this run.
        The resolved identity is frozen onto the created ``PlanState`` as
        ``creator_sender_info`` so a later approval/execution reads the
        plan's author, not whoever is cached (or merely clicked Approve) at
        that later point.
        """
        import uuid

        user = (
            sender_info if sender_info is not None else (self._resolve_user(chat_id, message) or {})
        )
        tenant_id = user.get("tenant_id") or self.config.tenant_id
        _identity = self._build_identity(user, chat_id, tenant_id)

        # Typing indicator
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        thinking_emoji = "\U0001f9e0" if deep_plan else "\U0001f4cb"
        thinking_label = "Gathering context for deep reasoning..." if deep_plan else "Planning..."
        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text=f"{thinking_emoji} {thinking_label}",
                parse_mode=None,
            )
            stream_msg_id: int | None = thinking_msg.message_id
        except Exception:
            stream_msg_id = None

        run_for_delivery: Any = None
        try:
            model = self._model_override.get(chat_id)
            history = list(session.history)

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=user_text,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan:{chat_id}",
                model_override=model,
                conversation_history=history or None,
                readonly_mode=True,
                deep_plan=deep_plan,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            plan_text = _extract_plan_text(run.output_text or "")

            # Accumulate history so revisions have full context
            session.history.append({"role": "user", "content": user_text})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Delete thinking message, deliver as new message
            if stream_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=stream_msg_id)

            if plan_text:
                plan = PlanState(
                    plan_id=str(uuid.uuid4()),
                    plan_text=plan_text,
                    original_message=user_text,
                    status="pending",
                    created_at=datetime.now(UTC).isoformat(),
                    exploration_run_id=run.id,
                    deep_plan=deep_plan,
                    creator_sender_info=user or None,
                )
                session.active_plan = plan

                # Persist plan state to DB
                get_task_registry().spawn(
                    save_plan_state_async(
                        session_key,
                        _plan_state_to_dict(plan),
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-plan:{chat_id}",
                )

                sent_plan = await self.send_message(chat_id, plan_text)
                await self._record_interactive_delivery(run, sent_plan)

                label = (
                    "<b>Approve this deep plan?</b>" if deep_plan else "<b>Approve this plan?</b>"
                )
                kb = self._build_plan_keyboard(plan.plan_id)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=label,
                    reply_markup=kb,
                )
            else:
                sent_fallback = await self.send_message(
                    chat_id, run.output_text or "No plan produced."
                )
                await self._record_interactive_delivery(run, sent_fallback)
        except Exception as e:
            logger.error("Plan mode failed: %s", e, exc_info=True)
            sent_error = await self.send_message(chat_id, f"Plan mode error: {html.escape(str(e))}")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            typing_active = False
            typing_task.cancel()

    async def _run_deep_mode(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        query: str,
        message: Message,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Execute deep reasoning via RLM, show progress edits and result.

        ``sender_info`` (Task 4 Finding 1 fix): same per-message-resolved
        threading pattern as ``_run_plan_mode`` — captured up front (before
        any await) and never re-read from the shared, chat_id-keyed
        ``_chat_user_info`` cache.
        """
        user = (
            sender_info if sender_info is not None else (self._resolve_user(chat_id, message) or {})
        )
        tenant_id = user.get("tenant_id") or self.config.tenant_id
        _identity = self._build_identity(user, chat_id, tenant_id)

        # Typing indicator
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text="\U0001f9e0 Deep reasoning...",
                parse_mode=None,
            )
            progress_msg_id: int | None = thinking_msg.message_id
        except Exception:
            progress_msg_id = None

        async def on_progress(progress: dict[str, Any]) -> None:
            nonlocal progress_msg_id
            elapsed = progress.get("elapsed_s", 0)
            text = f"\U0001f9e0 Deep reasoning... {elapsed}s elapsed"
            try:
                if progress_msg_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=progress_msg_id,
                        text=text,
                        parse_mode=None,
                    )
            except Exception:
                pass

        run_for_delivery: Any = None
        try:
            history = list(session.history)

            run = await self.runner.execute_deep(
                query=query,
                on_progress=on_progress,
                conversation_history=history or None,
                trigger_type=TriggerType.TELEGRAM,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            # Record in session history
            session.history.append({"role": "user", "content": f"/deep {query}"})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Deep reasoning failed: {run.error_message}]",
                    }
                )
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Persist exchange to DB
            if run.output_text:
                get_task_registry().spawn(
                    save_exchange_async(
                        session_key,
                        f"/deep {query}",
                        run.output_text,
                        channel="telegram",
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-deep:{chat_id}",
                )

            # Delete progress message, deliver result as new message
            if progress_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=progress_msg_id)

            if run.output_text:
                duration_s = (run.duration_ms or 0) / 1000
                cost_str = f"${run.total_cost_usd:.2f}" if run.total_cost_usd else "$?.??"
                footer = f"\n\n<i>RLM: {duration_s:.1f}s / {cost_str}</i>"
                sent_result = await self.send_message(chat_id, run.output_text)
                await self._record_interactive_delivery(run, sent_result)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=footer,
                )
            elif run.error_message:
                sent_failure = await self.send_message(
                    chat_id,
                    f"\u274c Deep reasoning failed: {html.escape(run.error_message)}",
                )
                await self._record_interactive_delivery(run, sent_failure)
        except Exception as e:
            logger.error("Deep mode failed: %s", e, exc_info=True)
            sent_error = await self.send_message(
                chat_id, f"Deep reasoning error: {html.escape(str(e))}"
            )
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            typing_active = False
            typing_task.cancel()

    async def _execute_approved_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Execute an approved plan in background mode.

        Runs as a fire-and-forget asyncio task (caller wraps in create_task
        -- the ``asyncio.create_task`` scheduling gap between the approval
        click and this coroutine's body actually running is exactly the
        window in which a group chat's shared ``_chat_user_info[chat_id]``
        cache can be overwritten by an unrelated sender's message (Task 4
        Finding 1). Attribution here instead comes from
        ``plan.creator_sender_info`` -- the identity frozen at plan-creation
        time in ``_run_plan_mode`` -- not whoever is cached for the chat, and
        not whoever happened to click "Approve" (any group member can click
        approve; the plan's author owns what it does end to end).

        Sends immediate acknowledgement, executes with continuous-mode overrides
        for long-running tasks, and delivers the final result as a new Telegram
        message (triggering a push notification).
        """
        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan to execute.")
            return

        # Check expiration before executing
        if _plan_is_expired(plan):
            plan.status = "expired"
            session.active_plan = None
            await self.send_message(
                chat_id, "Plan has expired. Please start a new plan with /plan."
            )
            return

        plan.status = "approved"

        user = plan.creator_sender_info or {}
        tenant_id = user.get("tenant_id") or self.config.tenant_id
        _identity = self._build_identity(user, chat_id, tenant_id)

        # Deep plan: route to RLM with rich context instead of agent execution
        if plan.deep_plan:
            await self._execute_deep_plan(chat_id, session_key, session)
            return

        # ── Immediate acknowledgement ──
        await self.send_message(
            chat_id,
            "\u2705 On it \u2014 executing in the background. "
            "I'll send progress updates and notify you when done. "
            "Send /stop to cancel.",
        )

        run_for_delivery: Any = None
        try:
            model = self._model_override.get(chat_id)

            # Build agent config with continuous-mode overrides for background execution
            bg_config = self._build_background_config()

            # CONTEXT RESET — clean execution context, no planning history.
            # The LLM only sees the plan + original request. This structurally
            # prevents re-planning (the agent never sees its own plan output
            # as part of a conversation it needs to continue).
            execution_message = (
                "Execute the following approved plan. "
                "Use your tools to carry out each step.\n"
                "Do NOT re-plan, re-draft, or produce another version. ACT.\n\n"
                f"Original request: {plan.original_message}\n\n"
                f"Approved plan:\n{plan.plan_text}"
            )

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=execution_message,
                agent_config=bg_config,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan-exec:{chat_id}",
                model_override=model,
                conversation_history=None,  # CLEAN CONTEXT
                execution_mode=True,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            # Track execution run ID on plan
            plan.execution_run_id = run.id

            # Merge execution result back into session history for follow-up continuity
            session.history.append(
                {"role": "user", "content": f"[Plan executed] {plan.original_message}"}
            )
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                session.history.append(
                    {"role": "assistant", "content": f"[Execution failed: {run.error_message}]"}
                )
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Persist to DB
            if run.output_text:
                get_task_registry().spawn(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="telegram",
                        model_override=model,
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-plan-exec:{chat_id}",
                )

            # Clear plan + persist
            session.active_plan = None
            get_task_registry().spawn(
                clear_plan_state_async(session_key, tenant_id=tenant_id),
                name=f"tg-clear-plan-exec:{chat_id}",
            )

            # Send final result as NEW message (not edit) so user gets push notification
            duration_s = (run.duration_ms or 0) / 1000
            cost_str = f"${run.total_cost_usd:.4f}" if run.total_cost_usd else "$0"
            footer = f"\n\n\u2014 {duration_s:.0f}s / {cost_str}"

            if run.output_text:
                sent_result = await self.send_message(chat_id, run.output_text + footer)
            elif run.error_message:
                sent_result = await self.send_message(
                    chat_id, f"Plan failed: {run.error_message}{footer}"
                )
            else:
                sent_result = await self.send_message(chat_id, f"Plan complete. No output.{footer}")
            await self._record_interactive_delivery(run, sent_result)
        except asyncio.CancelledError:
            logger.info("Background plan execution cancelled for chat %s", chat_id)
            sent_cancel = await self.send_message(chat_id, "Plan execution cancelled.")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_cancel)
        except Exception as e:
            logger.error("Background plan execution failed: %s", e, exc_info=True)
            sent_error = await self.send_message(chat_id, f"Execution error: {html.escape(str(e))}")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            self._active_tasks.pop(chat_id, None)

    async def _execute_deep_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Execute approved deep plan — route to RLM with rich context.

        Attribution comes from ``plan.creator_sender_info`` (Task 4 Finding
        1), same reasoning as ``_execute_approved_plan``: this runs off a
        create_task fire-and-forget schedule and must not read whichever
        sender happens to be cached for the chat by the time it executes.
        """
        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan.")
            return

        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text="\U0001f9e0 Deep reasoning...",
                parse_mode=None,
            )
            progress_msg_id: int | None = thinking_msg.message_id
        except Exception:
            progress_msg_id = None

        async def on_progress(progress: dict[str, Any]) -> None:
            nonlocal progress_msg_id
            elapsed = progress.get("elapsed_s", 0)
            text = f"\U0001f9e0 Deep reasoning... {elapsed}s elapsed"
            try:
                if progress_msg_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=progress_msg_id,
                        text=text,
                        parse_mode=None,
                    )
            except Exception:
                pass

        run_for_delivery: Any = None
        try:
            # Build rich context from plan + exploration output
            user = plan.creator_sender_info or {}
            tenant_id = user.get("tenant_id") or self.config.tenant_id
            _identity = self._build_identity(user, chat_id, tenant_id)
            exploration_output = ""
            for msg in reversed(session.history):
                if msg.get("role") == "assistant" and msg.get("content"):
                    exploration_output = msg["content"]
                    break

            context = (
                f"Original request: {plan.original_message}\n\n"
                f"Research plan:\n{plan.plan_text}\n\n"
                f"Exploration output:\n{exploration_output}"
            )

            run = await self.runner.execute_deep(
                query=plan.original_message,
                on_progress=on_progress,
                context_override=context,
                trigger_type=TriggerType.TELEGRAM,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            # Track execution run ID
            plan.execution_run_id = run.id

            # Record in session history
            session.history.append(
                {"role": "user", "content": f"[Deep plan executed] {plan.original_message}"}
            )
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Deep reasoning failed: {run.error_message}]",
                    }
                )
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Persist exchange to DB
            if run.output_text:
                get_task_registry().spawn(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="telegram",
                        model_override=self._model_override.get(chat_id),
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-deep-exec:{chat_id}",
                )

            # Clear plan + persist
            session.active_plan = None
            get_task_registry().spawn(
                clear_plan_state_async(session_key, tenant_id=tenant_id),
                name=f"tg-clear-deep-exec:{chat_id}",
            )

            # Delete progress message, deliver result as new message
            if progress_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=progress_msg_id)

            if run.output_text:
                duration_s = (run.duration_ms or 0) / 1000
                cost_str = f"${run.total_cost_usd:.2f}" if run.total_cost_usd else "$?.??"
                footer = f"\n\n<i>RLM: {duration_s:.1f}s / {cost_str}</i>"
                sent_result = await self.send_message(chat_id, run.output_text)
                await self._record_interactive_delivery(run, sent_result)
                await self.bot.send_message(chat_id=int(chat_id), text=footer)
            elif run.error_message:
                sent_failure = await self.send_message(
                    chat_id,
                    f"\u274c Deep reasoning failed: {html.escape(run.error_message)}",
                )
                await self._record_interactive_delivery(run, sent_failure)
        except Exception as e:
            logger.error("Deep plan execution failed: %s", e, exc_info=True)
            sent_error = await self.send_message(
                chat_id, f"Deep reasoning error: {html.escape(str(e))}"
            )
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            typing_active = False
            typing_task.cancel()

    async def _iterate_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        feedback: str,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Revise the active plan based on user feedback (keeps same plan_id).

        ``sender_info`` (Task 4 Finding 1 fix): the per-message resolved
        identity of whoever sent this feedback text — threaded straight
        through from ``handle_text``, exactly like ``_run_interactive``'s
        ``sender_info``. Unlike the approval paths, revision feedback is
        attributed to whoever is actively typing right now (same as plan
        creation itself), not frozen to the plan's original creator — this
        is still a read-only exploration step, not the privileged execution
        phase. Falls back to the legacy ``_chat_user_info[chat_id]`` cache
        read only when the caller doesn't thread anything through (keeps any
        direct caller byte-identical).
        """

        user = sender_info if sender_info is not None else (self._chat_user_info.get(chat_id) or {})
        tenant_id = user.get("tenant_id") or self._get_tenant_id(chat_id)
        _identity = self._build_identity(user, chat_id, tenant_id)

        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan to revise.")
            return

        # Check expiration before iterating
        if _plan_is_expired(plan):
            plan.status = "expired"
            session.active_plan = None
            await self.send_message(
                chat_id, "Plan has expired. Please start a new plan with /plan."
            )
            return

        # Save current plan text to revision history
        plan.revision_history.append(
            {
                "plan_text": plan.plan_text,
                "feedback": feedback,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        plan.revision_count += 1

        # Typing indicator
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text=f"\u270f\ufe0f Revising plan (v{plan.revision_count + 1})...",
                parse_mode=None,
            )
            stream_msg_id: int | None = thinking_msg.message_id
        except Exception:
            stream_msg_id = None

        run_for_delivery: Any = None
        try:
            model = self._model_override.get(chat_id)

            # Build iteration prompt with current plan + feedback
            iteration_message = (
                "[PLAN REVISION]\n"
                "The user reviewed your plan and gave this feedback:\n"
                f'"{feedback}"\n\n'
                f"Current plan:\n{plan.plan_text}\n\n"
                "Revise the plan to address their feedback. "
                "Keep everything they didn't object to.\n"
                'Start with "Changes:" summarizing what you changed.\n'
                "End with [PLAN_READY]."
            )

            history = list(session.history)

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=iteration_message,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan-revise:{chat_id}",
                model_override=model,
                conversation_history=history or None,
                readonly_mode=True,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            revised_plan_text = _extract_plan_text(run.output_text or "")

            # Update history
            session.history.append({"role": "user", "content": feedback})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Delete thinking message, deliver as new message
            if stream_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=stream_msg_id)

            if revised_plan_text:
                # Update plan in-place (same plan_id)
                plan.plan_text = revised_plan_text

                revision_label = f"<b>Plan v{plan.revision_count + 1}</b>"
                sent_revision = await self.send_message(chat_id, revised_plan_text)
                await self._record_interactive_delivery(run, sent_revision)

                kb = self._build_plan_keyboard(plan.plan_id, plan.revision_count)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"{revision_label} — Approve this plan?",
                    reply_markup=kb,
                )

                # Persist updated plan state
                get_task_registry().spawn(
                    save_plan_state_async(
                        session_key,
                        _plan_state_to_dict(plan),
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-plan-revision:{chat_id}",
                )
            else:
                sent_fallback = await self.send_message(
                    chat_id, run.output_text or "No revised plan produced."
                )
                await self._record_interactive_delivery(run, sent_fallback)
        except Exception as e:
            logger.error("Plan iteration failed: %s", e, exc_info=True)
            sent_error = await self.send_message(chat_id, f"Revision error: {html.escape(str(e))}")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            typing_active = False
            typing_task.cancel()
