"""The Helm as a place output goes, and a place a question can be answered.

Until now ``webchat`` was a *trigger* and never a destination: a member could
talk to an agent through the Helm, but no agent could reach a member there.
``delivery.channel`` accepted the name and ``get_channel("webchat")`` returned
``None``, so a manifest that asked for it recorded ``failed:no_channel:webchat``
— which is honest, and useless to the operator who wanted a member briefed.

What "delivered" means here
---------------------------
Two rows, because a webchat message has two halves and each can fail alone:

* the assistant turn in the member's own chat session (``chat_sessions`` +
  ``chat_messages``, through ``chat_store.save_channel_surface``), which is what
  they read in the Helm and what the agent's next turn sees as history, and
* the notification in their inbox (``crm_agent_notifications``), which is the
  only half visible to somebody who does not have the Helm open.

So ``expected`` is 2 and ``acknowledged`` counts the ids the two writers
actually returned. One of the two is a ``partial:``, named specifically —
``failed:webchat_no_session_write`` or ``failed:webchat_no_notification`` — so
an operator can query which half is broken rather than inferring it. Neither
writer raises: ``save_channel_surface_async`` logs and returns ``None``, and
``dal.send_notification`` rolls back and returns ``None``. The ids are therefore
the only evidence that anything landed, exactly as
:mod:`robothor.engine.channels.base` insists.

Which session
-------------
``target`` is a ``user_accounts.id``, and the session key is derived from it by
:func:`robothor.engine.chat.derive_user_session_key` — the same function the
request path uses, so a delivery lands in the session the member's own messages
land in. The owner is the exception, in the same direction as everywhere else:
they keep ``EngineConfig.main_session_key``, which is the session
``engine/telegram.py`` also writes into, so a briefing reaches the operator in
the conversation they already have rather than a second one beside it.

Asking
------
There is no inbound webchat socket, so ``ask`` cannot wait on a reply arriving
at this process. It waits on the ROW instead: the ``approval_required`` event
goes out over the run's own SSE stream, the browser answers through the bridge's
``POST /api/approvals/question/{id}`` (which settles ``agent_questions``), and
this polls the row it already owns until it is answered. No new endpoint, and no
second source of truth about what the answer was.

The emit is idempotent by row id — ``ask_user`` emits the same event before
calling any channel, and the Helm keys its card on ``id`` — so a caller that has
not already announced the question still reaches the browser, and one that has
does not produce a second card.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from robothor.engine import agent_questions, chat, chat_store, run_status
from robothor.engine.channels.base import SendReceipt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["POLL_INTERVAL_S", "WebchatChannel"]

#: How often an open ask re-reads its row. One small indexed query per open
#: question — there is no notification channel from the bridge to the engine,
#: and adding an endpoint for sub-second settling is deferred, not forgotten.
POLL_INTERVAL_S = 2.0

#: The subject line's ceiling. A notification subject is rendered in a list, so
#: the whole body in it is a wall; the body carries the text in full.
MAX_SUBJECT = 120


def _send_notification(**kw: Any) -> str | None:
    """``dal.send_notification``, reached through one seam.

    Imported lazily and wrapped so the DB layer is not pulled in at channel
    registration time (``_ensure_builtins`` runs on the first lookup, in
    whatever process that is) and so the suite can replace the write without
    patching the CRM DAL for every other caller in the process.
    """
    from robothor.crm import dal

    return dal.send_notification(**kw)


def _resolve_identity(channel: str, identifier: str, tenant: str) -> Any:
    """``robothor.identity.resolve_identity``, same seam and same reason."""
    from robothor.identity import resolve_identity

    return resolve_identity(channel, identifier, tenant)


def _probe_database() -> bool:
    """Whether this process can currently reach Postgres. Read-only."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
    return True


def _tenant_id() -> str:
    """The tenant a delivery is written under.

    Read off the engine's own config rather than the environment, so this
    module adds no raw env reader.
    """
    from robothor.constants import DEFAULT_TENANT

    config = getattr(chat, "_config", None)
    return str(getattr(config, "tenant_id", "") or DEFAULT_TENANT)


class WebchatChannel:
    """A member's Helm session and inbox, reachable as ``delivery.channel: webchat``.

    Registered on every instance and always configured: the Helm is part of the
    platform, not an integration an operator sets up, so there is no
    ``failed:webchat_not_configured`` and no ``verify()`` — see the channel's
    doc page for why ``genus channel verify webchat`` exits 2.
    """

    name = "webchat"

    #: Inbound webchat is the ``/chat`` router in ``engine/chat.py``, not a
    #: router this channel owns.
    inbound_router: Any | None = None

    #: The capability ``ask_user`` probes for before passing the durable row's
    #: id into :meth:`ask`. Telegram and Slack do not set it, so their ``ask``
    #: signatures — which take no ``**kw`` — are never handed a kwarg they
    #: cannot accept; an unconditional one would raise ``TypeError``, be
    #: swallowed as "the channel could not ask", and silently break every
    #: Telegram ask.
    ask_wants_question_id = True

    async def start(self) -> None:
        """No-op: the Helm's transport is the engine's own HTTP server."""
        return

    async def stop(self) -> None:
        """No-op: see :meth:`start`."""
        return

    async def health(self) -> dict[str, Any]:
        """What the operator needs to see. Never raises (a diagnostic that
        raises on a broken box helps nobody), and names no member."""
        report: dict[str, Any] = {
            "channel": self.name,
            "configured": True,
            "ok": False,
            "sessions": chat.session_count(),
        }
        try:
            report["ok"] = bool(await asyncio.to_thread(_probe_database))
        except Exception as exc:  # noqa: BLE001 — health never raises
            report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    # ── send ─────────────────────────────────────────────────────────────

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Write ``text`` into ``target``'s session and inbox.

        Guards first, in Slack's order: a manifest defect is a manifest defect
        whether or not the member exists, and saying "unknown user" about an
        unexpanded ``${HELM_USER}`` sends the operator after the wrong thing.
        """
        clean = (target or "").strip()
        agent_id = str(getattr(config, "id", "") or "")
        if not clean:
            logger.warning("No webchat delivery target for %s", agent_id or "?")
            return SendReceipt(
                acknowledged=0, expected=2, status="failed:webchat_no_target", target=clean
            )
        if "${" in clean:
            logger.error(
                "Unexpanded env var in the webchat delivery target for %s", agent_id or "?"
            )
            return SendReceipt(
                acknowledged=0, expected=2, status="failed:webchat_unexpanded_target", target=clean
            )

        tenant_id = _tenant_id()
        identity = await asyncio.to_thread(_resolve_identity, "webchat", clean, tenant_id)
        if identity is None:
            # Not "no session": the address does not belong to an active
            # account in this tenant, so writing a turn would create a session
            # nobody can ever open.
            logger.error(
                "Webchat delivery for %s names a user_accounts.id that is not an active "
                "account in this tenant",
                agent_id or "?",
            )
            return SendReceipt(
                acknowledged=0, expected=2, status="failed:webchat_unknown_user", target=clean
            )

        session_key = self._session_key_for(identity, clean, agent_id)
        name = str(getattr(config, "name", "") or "")
        body = f"{name}\n\n{text}" if name else text

        message_id = await chat_store.save_channel_surface_async(
            session_key,
            body,
            agent_id,
            author_display_name=name,
            surfaced_from_run_id=getattr(run, "id", None),
            channel=self.name,
            tenant_id=tenant_id,
        )
        notification_id = await asyncio.to_thread(
            _send_notification,
            from_agent=agent_id,
            to_agent=clean,
            notification_type="info",
            subject=self._subject(name, text),
            body=text,
            metadata={
                "kind": "webchat_delivery",
                "session_key": session_key,
                "run_id": getattr(run, "id", None),
                "chat_message_id": str(message_id) if message_id is not None else None,
            },
            tenant_id=tenant_id,
        )

        if message_id is not None:
            # Only after the row is written. The Helm's history endpoint reads
            # RAM and the DB only at boot, so a turn missing from the session
            # would be invisible until the next restart — but a turn in RAM
            # that no row backs would vanish at that restart instead.
            chat.get_shared_session(session_key).history.append(
                {"role": "assistant", "content": body}
            )

        return self._receipt(clean, body, message_id, notification_id, agent_id)

    @staticmethod
    def _session_key_for(identity: Any, target: str, agent_id: str) -> str:
        """The session this delivery belongs in.

        The owner keeps the shared key (``engine/telegram.py`` writes there too,
        and splitting the operator's conversation in two is the one thing the
        per-user rollout may not do). Everybody else gets their own, derived by
        the same function the request path uses.
        """
        if str(getattr(identity, "role", "")) == "owner":
            return chat.get_main_session_key()
        return chat.derive_user_session_key(agent_id, target)

    @staticmethod
    def _subject(agent_name: str, text: str) -> str:
        """A one-line subject: the agent's name, or the body's first line."""
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        subject = agent_name or first or "Message"
        return subject[:MAX_SUBJECT]

    def _receipt(
        self,
        target: str,
        body: str,
        message_id: int | None,
        notification_id: str | None,
        agent_id: str,
    ) -> SendReceipt:
        """What the two writers proved, and nothing beyond it."""
        ids = [str(i) for i in (message_id, notification_id) if i is not None]
        status: str | None = None
        if message_id is None and notification_id is None:
            status = "failed:webchat_send"
            logger.error(
                "Webchat delivery for %s wrote neither the chat turn nor the notification — "
                "nobody saw it",
                agent_id or "?",
            )
        elif message_id is None:
            status = "failed:webchat_no_session_write"
            logger.error(
                "Webchat delivery for %s reached the inbox but not the chat session",
                agent_id or "?",
            )
        elif notification_id is None:
            status = "failed:webchat_no_notification"
            logger.error(
                "Webchat delivery for %s reached the chat session but not the inbox",
                agent_id or "?",
            )
        return SendReceipt(
            acknowledged=len(ids),
            expected=2,
            platform_ids=ids,
            status=status,
            target=target,
            body=body,
        )

    # ── ask ──────────────────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
        question_id: str = "",
        run_id: str = "",
    ) -> str | None:
        """Put ``question`` to the Helm and wait for the row to be settled.

        ``question_id`` is the ``agent_questions`` row this waits on, passed in
        because :data:`ask_wants_question_id` is set. Without one there is no
        return path at all, so this raises ``NotImplementedError`` — the
        documented "there is no way to ask here" outcome — rather than returning
        ``None``, which would claim a person was asked and stayed silent.

        Returns the answer, or ``None`` for the deadline passing or the row
        being expired. **Never one of ``options``**: an option returned because
        the clock ran out is an approval nobody gave.
        """
        if not question_id:
            raise NotImplementedError(
                "the webchat channel answers through the agent_questions row, so it cannot "
                "ask without one; the caller must record the question first"
            )

        tenant_id = _tenant_id()
        try:
            row = await asyncio.to_thread(
                agent_questions.get_question, question_id, tenant_id=tenant_id
            )
        except Exception as exc:  # noqa: BLE001 — an unreadable row is no return path
            raise NotImplementedError(
                f"the webchat channel could not read question {question_id}, so it has no "
                "way to carry an answer back"
            ) from exc
        if row is None:
            raise NotImplementedError(
                f"question {question_id} does not exist in this tenant, so there is nothing "
                "for the webchat channel to wait on"
            )

        await run_status.emit_status(
            run_id,
            {
                "event": "approval_required",
                "kind": "question",
                "id": question_id,
                "run_id": run_id,
                "question": question,
                "options": list(options),
                "target": target,
                "addressee": addressee,
                "expires_at": (
                    row.expires_at.isoformat() if getattr(row, "expires_at", None) else None
                ),
            },
        )
        return await self._await_answer(row, question_id, tenant_id, timeout)

    async def _await_answer(
        self, row: Any, question_id: str, tenant_id: str, timeout: float
    ) -> str | None:
        """Poll the row until it is settled, expired, or the deadline passes.

        The sleep is clamped to whatever is left of the budget rather than being
        a flat interval: ``ask_user``'s timeout arithmetic is load-bearing (the
        tool registry wraps the call in its own ``asyncio.timeout``), and a wait
        that overshot by an interval would spend headroom that exists to keep
        "nobody answered" — a fact worth reporting — from becoming a tool
        failure.

        ``CancelledError`` is deliberately not caught: a run being torn down
        must not look like a person declining to answer.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            status = str(getattr(row, "status", "") or "")
            if status == "answered":
                answer = getattr(row, "answer", None)
                return str(answer) if answer is not None else None
            if status == "expired":
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(POLL_INTERVAL_S, remaining))
            try:
                row = await asyncio.to_thread(
                    agent_questions.get_question, question_id, tenant_id=tenant_id
                )
            except Exception:  # noqa: BLE001 — a blip is not an answer
                logger.warning("Could not re-read question %s; still waiting", question_id)
                continue
            if row is None:
                return None

    async def resolve_identity(self, native_id: str) -> Any:
        """Not this channel's job: the Helm's inbound half resolves identity per
        request in ``engine/chat.py`` (``_resolve_webchat_identity``), from the
        authenticated session rather than from a platform-native id."""
        raise NotImplementedError(
            "webchat identity is resolved from the authenticated request in engine/chat.py, "
            "not from a channel-native sender id"
        )
