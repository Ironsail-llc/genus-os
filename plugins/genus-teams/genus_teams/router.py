"""The messaging endpoint — the one public thing this distribution publishes.

``POST /api/channels/teams/messages``. The engine mounts it only when the
operator has armed ``teams``, and only under that path; see
``robothor/engine/channels/routers.py``.

The order of the checks is the design
-------------------------------------
Cheap and unauthenticated first, expensive and authenticated last, because
everything before the signature check is reachable by anybody who finds the URL:

1. **size** — streamed, with an early abort. ``await request.json()`` buffers
   the whole body before anything can object, so a 5 MB activity would already
   be in memory by the time a cap ran;
2. **rate** — a token bucket, so a flood costs a dictionary lookup rather than
   two key fetches and a JSON parse;
3. **signature and claims** — :mod:`genus_teams.jwt_validation`, including the
   ``serviceurl`` binding;
4. **the access gate** — ``channels.access.evaluate``, which decides whether
   this sender may drive an agent at all;
5. **the run** — through ``channels.inbound.handle_message``, the pipeline Slack
   uses. Not a copy of it: see that module for why a second copy is the defect.

Acknowledge first, answer later
-------------------------------
Teams abandons the request after about 15 seconds and a real run routinely takes
longer, so the endpoint returns 200 as soon as the activity is recorded and does
the rest in a background task, replying through the outbound channel. A
synchronous answer would be an endpoint that times out on every useful question
and then gets the same activity delivered again.

What it never does
------------------
Trust anything in the body that the signed token also states (the service URL),
log a message or a person's id, or tell a refused caller which check refused it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, Request, Response

from genus_teams.credentials import teams_credentials
from genus_teams.jwt_validation import TokenRejectedError, validate_activity_token
from robothor.engine.channels import access, conversations

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ACTIVITY_BYTES",
    "RATE_LIMIT_PER_MINUTE",
    "build_router",
    "reset_rate_limit",
    "spawn",
]

#: The route, spelled once. The engine refuses to mount a plugin channel's
#: router anywhere outside ``/api/channels/<name>``.
PATH = "/api/channels/teams/messages"

#: A Teams message activity without attachments is a few kilobytes. This is
#: generous for one with cards and mentions and nowhere near enough to be worth
#: sending as a denial of service.
MAX_ACTIVITY_BYTES = 256 * 1024

#: A conversation with a bot is a person typing. Anything past this is a script,
#: and the platform's own retries stay well inside it.
RATE_LIMIT_PER_MINUTE = 120

#: In-process, like every other limiter on this appliance
#: (``auth/local_login.py``, ``engine/channel_bus.py``). A shared counter would
#: need Redis, which the engine has but a plugin should not assume.
_hits: list[float] = []
_clock = time.monotonic


def reset_rate_limit() -> None:
    """Empty the bucket. For tests, and for a reload."""
    _hits.clear()


def _rate_limited() -> bool:
    """Whether this request is over the minute's budget."""
    now = _clock()
    cutoff = now - 60.0
    while _hits and _hits[0] < cutoff:
        _hits.pop(0)
    if len(_hits) >= RATE_LIMIT_PER_MINUTE:
        return True
    _hits.append(now)
    return False


def spawn(coro: Any) -> Any:
    """Run a coroutine after the response. The seam the suite replaces.

    A module-level function rather than ``asyncio.create_task`` inline so a test
    can see what the endpoint deferred, and so the reference is held: a bare
    ``create_task`` result that nobody keeps can be garbage-collected mid-run.
    """
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


_background: set[asyncio.Task[Any]] = set()

#: ``<at>Genus</at>`` and friends. Stripping the bot's own mention is the whole
#: of the "message parsing" this channel does: everything else the person typed
#: is theirs, and a channel that rewrote it would be answering a question
#: nobody asked.
_MENTION = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)


async def _bounded_body(request: Request) -> bytes | None:
    """The body, or ``None`` when it is over the cap.

    Declared ``Content-Length`` first (free), then streamed with an early abort
    for a chunked request that declares none. The pattern
    ``crm/bridge/routers/auth.py`` uses, for its reason: a cap applied after
    ``await request.json()`` has already paid for the thing it is refusing.
    """
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_ACTIVITY_BYTES:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_ACTIVITY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _sender(activity: dict[str, Any]) -> tuple[str, str]:
    """``(native id, display name)`` for whoever sent this.

    ``aadObjectId`` — the person in the directory — in preference to ``from.id``,
    which is per-installation and changes when somebody reinstalls Teams. A
    pairing binds the id that survives. A guest or an anonymous meeting
    participant has no directory id at all, and falls back to ``from.id`` rather
    than being refused: they are still somebody, and a channel that worked for
    staff and silently not for guests would be diagnosed as broken.
    """
    sender = activity.get("from") or {}
    if not isinstance(sender, dict):
        return "", ""
    native = str(sender.get("aadObjectId") or "").strip() or str(sender.get("id") or "").strip()
    return native, str(sender.get("name") or "")


def _surface(activity: dict[str, Any]) -> str:
    """Whether a pairing code may be sent here.

    Teams' own ``conversationType`` is the authority: ``personal`` is a 1:1
    chat. Anything else — a channel, a group chat, a meeting — is a room, and a
    code posted in a room is a code anyone in it can carry to the operator.
    Unknown counts as a room: fail closed.
    """
    conversation = activity.get("conversation") or {}
    kind = str(conversation.get("conversationType") or "") if isinstance(conversation, dict) else ""
    return access.DIRECT_SURFACE if kind == "personal" else access.GROUP_SURFACE


def _text(activity: dict[str, Any]) -> str:
    """What the person actually said, with the bot's own mention removed."""
    return _MENTION.sub("", str(activity.get("text") or "")).strip()


def _is_from_the_bot(activity: dict[str, Any], app_id: str | None) -> bool:
    """Whether this activity is the bot's own. A bot answering itself is a loop."""
    sender = activity.get("from") or {}
    if not isinstance(sender, dict):
        return False
    identifier = str(sender.get("id") or "")
    return bool(app_id) and (identifier == f"28:{app_id}" or identifier == str(app_id))


def build_router(channel: Any) -> APIRouter:
    """The router for ``channel``. One route, and everything it needs is on it."""
    router = APIRouter(tags=["channels"])

    @router.post(PATH)
    async def messages(request: Request) -> Response:  # pragma: no cover - exercised via HTTP
        body = await _bounded_body(request)
        if body is None:
            return Response(status_code=413)
        if _rate_limited():
            logger.warning("Teams: refusing activities, over %d/min", RATE_LIMIT_PER_MINUTE)
            return Response(status_code=429)

        credentials = teams_credentials(tenant_id=channel.tenant_id)
        try:
            activity = _parse(body)
            await validate_activity_token(
                request.headers.get("authorization"),
                activity,
                app_id=credentials.app_id,
            )
        except TokenRejectedError:
            # No body, and the same answer for every reason: an error that told
            # a caller WHICH check failed would be an oracle.
            return Response(status_code=401)
        except ValueError:
            return Response(status_code=400)

        if activity.get("type") != "message" or _is_from_the_bot(activity, credentials.app_id):
            return Response(status_code=200)

        native_id, display_name = _sender(activity)
        conversation = activity.get("conversation") or {}
        conversation_id = (
            str(conversation.get("id") or "") if isinstance(conversation, dict) else ""
        )
        if not native_id or not conversation_id:
            return Response(status_code=200)

        # Recorded BEFORE the gate: the pairing code this sender is about to be
        # sent has nowhere to go otherwise. A reference is routing, not a grant
        # — see the conversation store's own module docstring.
        await asyncio.to_thread(
            _record,
            channel,
            native_id,
            conversation_id,
            str(activity.get("serviceUrl") or ""),
            display_name,
        )

        if _settles_an_ask(channel, activity, native_id, conversation_id):
            return Response(status_code=200)

        spawn(
            _handle(
                channel,
                text=_text(activity),
                native_id=native_id,
                display_name=display_name,
                conversation_id=conversation_id,
                surface=_surface(activity),
            )
        )
        return Response(status_code=200)

    return router


def _parse(body: bytes) -> dict[str, Any]:
    import json

    try:
        activity = json.loads(body or b"{}")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("not JSON") from exc
    if not isinstance(activity, dict):
        raise ValueError("not an activity")
    return activity


def _record(
    channel: Any, native_id: str, conversation_id: str, service_url: str, display_name: str
) -> None:
    """Write the conversation reference. A failure here is logged, not fatal.

    The activity has still been authenticated, and refusing to answer somebody
    because a write failed would turn a database hiccup into a silent outage on
    a surface that looks healthy.
    """
    try:
        conversations.record(
            channel.name,
            native_id,
            tenant_id=channel.tenant_id,
            conversation_id=conversation_id,
            service_url=service_url,
            display_name=display_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Teams: could not record a conversation reference: %s", type(exc).__name__)


def _settles_an_ask(
    channel: Any, activity: dict[str, Any], native_id: str, conversation_id: str
) -> bool:
    """Whether this activity answered a pending question. See :mod:`genus_teams.ask`."""
    from genus_teams import ask as ask_module

    return ask_module.settle_from_activity(
        activity, conversation_id=conversation_id, native_id=native_id
    )


async def _handle(
    channel: Any,
    *,
    text: str,
    native_id: str,
    display_name: str,
    conversation_id: str,
    surface: str,
) -> None:
    """Gate, run, and reply — after the platform has already been acknowledged."""
    from robothor.engine.channels.inbound import handle_message
    from robothor.engine.models import TriggerType

    runner = getattr(channel, "runner", None)
    if runner is None:
        # Mounting refuses a channel that could not be bound, so this is the
        # belt to that braces: an endpoint that answered 200 and dropped every
        # message would look exactly like a working install.
        logger.error("Teams: no runner is bound, so an authenticated message went nowhere")
        return

    result = await handle_message(
        channel=channel.name,
        native_id=native_id,
        text=text,
        runner=runner,
        tenant_id=channel.tenant_id,
        session_key=f"agent:main:teams:{conversation_id}",
        trigger_type=TriggerType.CHANNEL,
        display_name=display_name,
        surface=surface,
    )
    # "" means send nothing: a suppressed pairing reply, or an unknown sender in
    # a room who must not learn that anybody is listening.
    if not result.reply:
        return
    await channel.send(native_id, result.reply)
