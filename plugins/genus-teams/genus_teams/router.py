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

from genus_teams import ask as ask_module
from genus_teams.credentials import teams_credentials
from genus_teams.jwt_validation import TokenRejectedError, validate_activity_token
from robothor.engine.channels import access, conversations

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ACTIVITY_BYTES",
    "PRE_AUTH_RATE_LIMIT_PER_MINUTE",
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
#: and the platform's own retries stay well inside it. Counted **per
#: conversation**, and only for a caller whose token has already been verified.
RATE_LIMIT_PER_MINUTE = 120

#: How many REJECTED requests one client address may make in a minute before it
#: is refused outright.
#:
#: A separate bucket, and — the part that matters — one that a caller carrying a
#: valid token never touches. The first cut counted every request in one bucket
#: before the token was checked, which was a denial of service that cost the
#: attacker nothing: anyone who knows the URL (it is in the Azure Bot
#: registration and in these docs) could hold the channel at 429 with junk while
#: every genuine activity from Microsoft was refused. Behind an ingress the
#: client address is the proxy's, so a bucket that refused BEFORE verifying
#: would have refused Microsoft too — which is exactly the bug, moved.
#:
#: So: verify first (bounded work — a size-capped body and one RS256 check
#: against a warm key cache), and spend this budget only on what failed. A flood
#: then costs the attacker a connection and this instance a signature check, and
#: costs a genuine activity nothing at all.
PRE_AUTH_RATE_LIMIT_PER_MINUTE = 240

#: How many distinct sources are tracked before the oldest are dropped. Bounded
#: because the key is chosen by the caller (an address, a conversation id), and
#: an unbounded dict keyed on something an attacker picks is a memory leak with
#: a nice name.
MAX_TRACKED_SOURCES = 4096

#: In-process, like every other limiter on this appliance
#: (``auth/local_login.py``, ``engine/channel_bus.py``). A shared counter would
#: need Redis, which the engine has but a plugin should not assume.
_hits: dict[str, list[float]] = {}
_pre_auth_hits: dict[str, list[float]] = {}
_clock = time.monotonic


def reset_rate_limit() -> None:
    """Empty both buckets. For tests, and for a reload."""
    _hits.clear()
    _pre_auth_hits.clear()


def _over_budget(buckets: dict[str, list[float]], key: str, limit: int) -> bool:
    """Whether ``key`` has spent its minute in ``buckets``. Prunes as it goes."""
    now = _clock()
    cutoff = now - 60.0
    window = [at for at in buckets.get(key, ()) if at >= cutoff]
    if len(window) >= limit:
        buckets[key] = window
        return True
    window.append(now)
    buckets[key] = window
    if len(buckets) > MAX_TRACKED_SOURCES:
        # Drop the coldest sources rather than growing without bound. A source
        # that is dropped simply starts its minute again, which is the right
        # failure: this is a throttle, not an accounting record.
        for stale in sorted(buckets, key=lambda k: buckets[k][-1])[: len(buckets) // 4]:
            buckets.pop(stale, None)
    return False


def _note_rejection(request: Request) -> bool:
    """Charge one rejected request to its client address.

    Returns True once that address is over :data:`PRE_AUTH_RATE_LIMIT_PER_MINUTE`
    rejections in a minute, which turns its 401s into 429s. Nothing a verified
    caller sends ever reaches here, so a flood cannot spend anybody else's
    budget — including the budget of the proxy address it shares with Microsoft.
    """
    client = request.client
    source = (client.host if client else "") or "unknown"
    return _over_budget(_pre_auth_hits, f"ip:{source}", PRE_AUTH_RATE_LIMIT_PER_MINUTE)


def _rate_limited(source: str) -> bool:
    """Whether this verified source is over the minute's budget."""
    return _over_budget(_hits, source or "unknown", RATE_LIMIT_PER_MINUTE)


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
            # a caller WHICH check failed would be an oracle. An address that
            # keeps failing is answered 429 instead — it costs this instance a
            # signature check per attempt and it should not get an unlimited
            # number of them.
            if _note_rejection(request):
                logger.warning(
                    "Teams: refusing rejected requests from one address, over %d/min",
                    PRE_AUTH_RATE_LIMIT_PER_MINUTE,
                )
                return Response(status_code=429)
            return Response(status_code=401)
        except ValueError:
            if _note_rejection(request):
                return Response(status_code=429)
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

        # The real budget, spent per CONVERSATION and only by a caller that has
        # proved itself. One noisy room cannot spend another room's minute, and
        # junk cannot spend anybody's.
        if _rate_limited(f"conversation:{conversation_id}"):
            logger.warning(
                "Teams: refusing activities from one conversation, over %d/min",
                RATE_LIMIT_PER_MINUTE,
            )
            return Response(status_code=429)

        # EVERYTHING else happens after the acknowledgement, including the
        # database write that records the conversation reference.
        #
        # Teams abandons the request at about 15 seconds. What is left on the
        # request path is a size-capped read and one signature check against a
        # warm key cache; a cold cache adds two bounded fetches, which is why
        # those are capped well inside the window. Holding the ack for a
        # `to_thread` DB round trip as well bought nothing: the reference is
        # needed by the work that follows it, not by the response.
        #
        # The reference is still written BEFORE the gate, because the pairing
        # code the gate mints is a message and this row is where it goes. A
        # reference is routing, not a grant — see the store's own docstring.
        # A process that dies in between loses one reference, and the sender's
        # next message writes it again.
        work = _dispatch(
            channel,
            activity,
            native_id=native_id,
            display_name=display_name,
            conversation_id=conversation_id,
            service_url=str(activity.get("serviceUrl") or ""),
            surface=_surface(activity),
        )
        spawn(work)
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


async def _dispatch(
    channel: Any,
    activity: dict[str, Any],
    *,
    native_id: str,
    display_name: str,
    conversation_id: str,
    service_url: str,
    surface: str,
) -> None:
    """Record the reference, then hand the activity to the right half.

    A card answer and a sentence are dispatched differently and BOTH go through
    the access gate; what they share is this row, which has to exist before
    either can reply.
    """
    await asyncio.to_thread(_record, channel, native_id, conversation_id, service_url, display_name)
    if ask_module.is_ask_submit(activity):
        await _settle(
            channel,
            activity,
            native_id=native_id,
            display_name=display_name,
            conversation_id=conversation_id,
            surface=surface,
        )
        return
    await _handle(
        channel,
        text=_text(activity),
        native_id=native_id,
        display_name=display_name,
        conversation_id=conversation_id,
        surface=surface,
    )


async def _settle(
    channel: Any,
    activity: dict[str, Any],
    *,
    native_id: str,
    display_name: str,
    conversation_id: str,
    surface: str,
) -> None:
    """Answer a pending card — but only for a sender the gate still allows.

    Settling an ask **resumes a run that is already going**, which is the same
    decision the access gate makes about starting one, arriving through a
    different door. Somebody paired when the question went out and revoked or
    denied since is exactly who that gate exists to stop, and a pending card is
    a live approval path until it is answered.

    The gate's own reply (a pairing code) is sent when it has one, so a stranger
    who presses a button is told the same thing as a stranger who says hello,
    rather than meeting a surface that silently does nothing for one input and
    answers another.
    """
    from genus_teams import ask as ask_module

    decision = await access.evaluate(
        channel.name,
        native_id,
        tenant_id=channel.tenant_id,
        display_name=display_name,
        surface=surface,
    )
    if not decision.allowed:
        logger.info("Teams: a card answer arrived from a sender the access gate refused")
        if decision.refusal:
            await channel.send(conversation_id, decision.refusal)
        return

    ask_module.settle_from_activity(
        activity,
        conversation_id=conversation_id,
        native_id=native_id,
        identity=decision.identity,
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
        reply_target=conversation_id,
        display_name=display_name,
        surface=surface,
    )
    # "" means send nothing: a suppressed pairing reply, or an unknown sender in
    # a room who must not learn that anybody is listening.
    if not result.reply:
        return
    # THE CONVERSATION, not the sender. `send(native_id)` resolves the sender's
    # most recent reference, and a run takes minutes: a second message from the
    # same person in a different chat moves that reference under the reply, so
    # the answer to a question asked in a shared room lands in a 1:1 chat, or
    # the other way round. The conversation an activity arrived in cannot move.
    await channel.send(conversation_id, result.reply)
