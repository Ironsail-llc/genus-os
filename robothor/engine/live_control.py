"""Steer, stop and progress for a conversation's live run, on any channel.

The engine can steer or halt a live run by ``run_id``
(:mod:`robothor.engine.interrupt_api`). A chat surface knows the conversation,
not the run, so this module adds the missing lookup and the pieces every
channel needs around it:

* :func:`conversation_scope` marks the run started inside it as the
  conversation's run. The mark is claimed once, when the run registers as live
  (``request_runtime.observe_request``), so a sub-agent the run spawns is never
  mistaken for it.
* :func:`find_live_run`, :func:`steer_conversation` and
  :func:`interrupt_conversation` reach that run, only within the caller's
  tenant, with ``interrupt_api``'s semantics: a steer joins at the next
  iteration boundary as an operator steering update, an interrupt ends the run
  there as CANCELLED.
* :func:`format_progress_line` is one compact line per ``tool_start``,
  ``tool_end`` and ``iteration_start`` event, with elapsed time, and never a
  tool's arguments or results. :class:`ProgressMessage` posts that line once
  and edits it in place, throttled, through two callables a channel supplies.

Channels expose all of this only with ``ROBOTHOR_STEER_EVERYWHERE`` on
(:func:`steer_everywhere_enabled`). Marking a run is unconditional and changes
nothing a user can see.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from robothor.engine import session_registry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "ProgressMessage",
    "claim_conversation",
    "conversation_scope",
    "find_live_run",
    "format_progress_line",
    "interrupt_conversation",
    "steer_conversation",
    "steer_everywhere_enabled",
]

#: The longest progress line any channel is handed.
MAX_LINE = 120

#: How long closing waits for a progress write already on its way, in seconds.
_WRITE_WAIT = 5.0

#: What a channel's ``/steer`` and ``/stop`` answer, the same words everywhere.
STEERING_REPLY = "Steering the run."
STOPPING_REPLY = "Stopping the run."
NOTHING_RUNNING = "Nothing is running here."
STEER_USAGE = "Usage: /steer <what the running task should change>"
STOP_FAILED = "Stop could not be confirmed."
STEER_FAILED = "Steer could not be delivered."

#: What a run's progress message reads once the run has ended.
PROGRESS_DONE = "Done."

_pending_key: ContextVar[tuple[str, list[str]] | None] = ContextVar(
    "live_conversation_key", default=None
)


def steer_everywhere_enabled() -> bool:
    """``ROBOTHOR_STEER_EVERYWHERE``, read through settings. False on any error."""
    try:
        from robothor.settings import get_settings

        return bool(get_settings().channels.steer_everywhere)
    except Exception:  # noqa: BLE001 - a settings error must not break a channel
        logger.debug("steer_everywhere setting unreadable; treating as off", exc_info=True)
        return False


# ── which run is the conversation's ──────────────────────────────────────


@contextlib.contextmanager
def conversation_scope(key: str, taken: list[str] | None = None) -> Iterator[list[str]]:
    """The next run that goes live inside this block belongs to ``key``.

    Yields the steers that run actually took, in order, filled as the loop
    absorbs them (``live_inbox.absorb_operator_steer``), so the channel can
    record them with the turn. A steer accepted but never read is not in it.
    Pass ``taken`` to have them collected into a list the caller already holds.
    """
    if taken is None:
        taken = []
    token = _pending_key.set((key, taken) if key else None)
    try:
        yield taken
    finally:
        _pending_key.reset(token)


def mark_turn(key: str) -> None:
    """:func:`conversation_scope` for a context that ends with its task.

    ``chat_control.start`` runs this inside the copied context of a webchat
    turn's task, so the run that task starts belongs to ``key``. Nothing is
    reset: the context is discarded with the task.
    """
    _pending_key.set((key, []) if key else None)


def claim_conversation(session: Any) -> None:
    """Give the run going live now the scope's key and steer log; later runs get none.

    A session that already holds a key (a verification retry entering again)
    keeps it.
    """
    pending = _pending_key.get()
    if pending is None:
        return
    _pending_key.set(None)
    session.conversation_key, session.steers_taken = pending


def find_live_run(key: str, *, tenant_id: str) -> str | None:
    """The run id of ``key``'s live run in ``tenant_id``, or ``None``."""
    if not key:
        return None
    for run_id in session_registry.active_run_ids():
        session = session_registry.lookup(run_id)
        if session is None or getattr(session, "conversation_key", None) != key:
            continue
        if getattr(session.run, "tenant_id", None) != tenant_id:
            continue
        return run_id
    return None


def steer_conversation(key: str, text: str, *, tenant_id: str) -> bool:
    """Steer ``key``'s live run. False when nothing runs there or ``text`` is blank."""
    from robothor.engine.interrupt_api import steer_session

    text = (text or "").strip()
    run_id = find_live_run(key, tenant_id=tenant_id) if text else None
    return run_id is not None and steer_session(run_id, text)


def interrupt_conversation(key: str, message: str | None = None, *, tenant_id: str) -> bool:
    """Halt ``key``'s live run at its next iteration boundary. False when none runs."""
    from robothor.engine.interrupt_api import interrupt_session

    run_id = find_live_run(key, tenant_id=tenant_id)
    return run_id is not None and interrupt_session(run_id, message)


async def steer_reply(key: str, text: str, *, tenant_id: str) -> str:
    """A channel's answer to ``/steer``: off the loop, and an answer even on failure."""
    try:
        found = await asyncio.to_thread(steer_conversation, key, text, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - the sender is told; the log has the cause
        logger.exception("A channel steer could not be delivered")
        return STEER_FAILED
    return STEERING_REPLY if found else NOTHING_RUNNING


async def stop_reply(key: str, *, tenant_id: str) -> str:
    """A channel's answer to ``/stop``: off the loop, and an answer even on failure."""
    try:
        found = await asyncio.to_thread(interrupt_conversation, key, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - the sender is told; the log has the cause
        logger.exception("A channel stop could not be recorded")
        return STOP_FAILED
    return STOPPING_REPLY if found else NOTHING_RUNNING


# ── progress ─────────────────────────────────────────────────────────────


def _label(tool: Any) -> str:
    return str(tool or "").replace("_", " ").strip() or "tool"


def _seconds(value: float) -> str:
    return f"{int(value)}s"


def format_progress_line(event: dict[str, Any], *, elapsed_s: float) -> str | None:
    """One compact line for a runner event, or ``None`` for an event with none.

    ``elapsed_s`` is the time since the run started. Arguments and results are
    never shown: a progress line goes to wherever the conversation is, which
    may be a room.
    """
    kind = event.get("event")
    elapsed = _seconds(elapsed_s)
    if kind == "tool_start":
        line = f"Running {_label(event.get('tool'))} · {elapsed}"
    elif kind == "tool_end":
        took = f"{float(event.get('duration_ms') or 0) / 1000:.1f}s"
        verb = "failed" if event.get("error") else "done"
        line = f"{_label(event.get('tool'))} {verb} in {took} · {elapsed}"
    elif kind == "iteration_start":
        line = f"Step {event.get('iteration', '?')} · {elapsed}"
    else:
        return None
    line = " ".join(line.split())
    if len(line) > MAX_LINE:
        line = line[: MAX_LINE - 1] + "…"
    return line


class ProgressMessage:
    """One progress message per run: posted on the first line, then edited.

    ``post(text)`` returns a handle for the message (``None`` when it did not
    land); ``edit(handle, text)`` replaces its text. Only the LATEST line is
    kept, and a single worker writes it no more often than ``min_interval``, so
    a burst of tool events costs one edit, not fifty. :meth:`close` writes the
    final state (or ``final``) once. A channel failure is logged and dropped:
    progress is never the reason a run fails.
    """

    def __init__(
        self,
        post: Callable[[str], Awaitable[Any]],
        edit: Callable[[Any, str], Awaitable[None]],
        *,
        min_interval: float = 1.0,
    ) -> None:
        self._post = post
        self._edit = edit
        self._min_interval = min_interval
        self._started = time.monotonic()
        self._latest: str | None = None
        self._written: str | None = None
        self._handle: Any = None
        self._posted = False
        self._next_allowed = 0.0
        self._worker: asyncio.Task[None] | None = None
        self._writing = False
        self._closed = False

    async def on_event(self, event: dict[str, Any]) -> None:
        """A runner ``on_tool``/``on_status`` event. Never raises, never waits."""
        if self._closed:
            return
        line = format_progress_line(event, elapsed_s=time.monotonic() - self._started)
        if line is None:
            return
        self._latest = line
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="channel-progress")

    async def close(self, final: str | None = None) -> None:
        """Stop the worker and write the final state once. Idempotent."""
        if self._closed:
            return
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            if self._writing:
                # A write is on its way to the platform: let it land, or the
                # message it creates is never finished (and never reads Done).
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(worker), timeout=_WRITE_WAIT)
            if not worker.done():
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await worker
        if not self._posted and self._latest is None:
            return  # nothing was ever shown; nothing to finish
        text = final or self._latest
        if text and text != self._written:
            await self._write(text)

    async def _run(self) -> None:
        while not self._closed:
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            line = self._latest
            if line is None or line == self._written:
                return
            await self._write(line)
            self._next_allowed = time.monotonic() + self._min_interval

    async def _write(self, text: str) -> None:
        self._writing = True
        try:
            if not self._posted:
                self._posted = True
                self._handle = await self._post(text)
            elif self._handle is not None:
                await self._edit(self._handle, text)
            else:
                return  # the first post never landed; never post a second message
            self._written = text
        except Exception as e:  # noqa: BLE001 - lost progress is not a lost answer
            logger.debug("Channel progress update failed: %s", type(e).__name__)
        finally:
            self._writing = False
