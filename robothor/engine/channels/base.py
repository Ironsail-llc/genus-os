"""The channel contract: what it means to reach a person, and to have proof.

A channel is one surface the instance talks to people over. Before this module
the surface was not an abstraction at all — ``delivery.deliver()`` dispatched on
the delivery *mode* and called ``_deliver_telegram`` for every announced run,
while ``AgentConfig.delivery_channel`` was parsed from the manifest, written to
``agent_schedules``, shown on the dashboard, and never once consulted to decide
where anything went.

The one rule this module exists to enforce
------------------------------------------
**A receipt is derived from what the sender returned. Never from reaching the
next line.** ``robothor/engine/CLAUDE.md`` states it as ``delivered =
bool(sent)``, and it is written down because the alternative shipped: the alert
pager read an HTTP 401 as a delivered page and 432+ notifications went nowhere
while every log line said "sent". ``TelegramBot.send_message`` is built the same
way — it retries a failed chunk as plain text and, when that fails too, logs and
returns a list that is simply *missing* that chunk (empty when every chunk
failed). It never raises. So the length of the returned list is the only
evidence that anybody saw anything.

``engine/slack.py`` is the standing counter-example: its registered sender
returns ``None``, so a channel built from it acknowledges nothing and reports
``failed:``. That is the correct outcome. A channel that assumed success because
the call did not raise would be a lie with a green test beside it.

Optional slots
--------------
:meth:`Channel.resolve_identity` maps a sender's platform-native id onto a Genus
identity. Telegram and Slack implement it; a channel that cannot answer raises
:exc:`NotImplementedError` rather than returning a plausible default, because a
channel that answered "yes" to an approval prompt nobody saw is worse than one
that refuses. ``None`` is a different answer again, and the one the access gate
is built on: *nobody is bound to this id*.

:meth:`Channel.ask` is now live on Telegram and is called by the ``ask_user``
tool and by ``permission_escalation.PermissionEscalationManager``. It stays
optional: ``EventBusChannel.ask`` raises, because a sink has nobody to ask, and
that raise is part of the contract rather than a gap in it — callers catch it
and fall through to the durable ``agent_questions`` row.

Because they are declared here, ``isinstance(x, Channel)`` means "implements
every slot including the optional two". The registry deliberately does not gate
on that — it requires only ``send``, so a plugin can ship a send-only channel.

What the platform drives today
------------------------------
``send`` only. **Nothing calls ``start``, ``stop`` or ``health``**: outbound
delivery resolves a channel and sends, and the daemon owns the Telegram bot's
own lifecycle directly. They are declared because a channel that receives needs
them and the shape should not change when the inbound half lands — but a channel
must open its transport lazily inside ``send`` rather than relying on ``start``
being called, or it will ship working tests and deliver nothing. Said plainly
here because a declared-and-inert extension point is this platform's most
frequent defect, and one that is documented is not a trap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "UNCONFIGURED_STEP",
    "Channel",
    "SendReceipt",
    "acknowledged_messages",
    "receipt_from",
]

#: How a channel's optional ``verify()`` says "this instance never set me up",
#: as opposed to "I tried and something is wrong": it returns exactly ONE step,
#: under this name, failed.
#:
#: The distinction is a different exit code from ``genus channel verify`` — 2
#: rather than 1 — and it is load-bearing: an instance that never wanted Slack
#: has not failed a check it did not ask for, and an install gate that treated
#: the two alike would fail every headless deployment. Naming the shape here,
#: rather than having the CLI ask ``health()`` separately, saves a second
#: authenticated round trip per run and stops a channel whose ``health`` raised
#: from being reported as verifiable.
UNCONFIGURED_STEP = "configuration"


@dataclass(frozen=True)
class SendReceipt:
    """What a channel can actually prove about one send.

    Attributes:
        acknowledged: how many chunks the platform confirmed. Counted from the
            sender's return value, never assumed.
        expected: how many chunks the body was split into.
        platform_ids: the platform's own message ids for the chunks that
            landed, so reply-to resolution still works for a partial send. May
            be shorter than ``acknowledged`` when a platform returns an object
            with no id; the count, not the ids, decides delivery.
        status: the exact ``agent_runs.delivery_status`` value this channel
            wants recorded, when it knows something the counts do not — a
            misconfiguration caught before the send
            (``failed:telegram_no_chat_id``), or a send that raised. Left
            ``None``, the status is derived from the counts by
            :func:`robothor.engine.delivery.apply_receipt`, so there is one
            mapping rather than one per channel.
        target: the address the send was aimed at, carried back so the
            POST_DELIVERY hook can record where the message landed.
        body: the text as the channel actually sent it, including whatever
            header the channel added. The channel bus records this, not the
            pre-formatting body.
        post_delivery: False for a sink with no per-recipient message to
            resolve a reply against (the event bus). Suppresses the
            POST_DELIVERY dispatch rather than recording a message id nobody
            can reply to.
    """

    acknowledged: int
    expected: int
    platform_ids: list[str] = field(default_factory=list)
    status: str | None = None
    target: str = ""
    body: str = ""
    post_delivery: bool = True

    @property
    def complete(self) -> bool:
        """True only when every expected chunk was acknowledged.

        ``acknowledged > 0`` is part of the test on purpose: an ``expected`` of
        zero must never make "nothing was sent" satisfy ">= expected".
        """
        return self.acknowledged > 0 and self.acknowledged >= self.expected


def acknowledged_messages(sent: Any) -> tuple[int, list[str]]:
    """Count the chunks the platform actually acknowledged.

    ``TelegramBot.send_message`` returns one entry per chunk it managed to
    send — a chunk that failed both the HTML and the plain-text attempt is
    simply absent from the list, so the length of the result is the only
    evidence of what landed.

    Args:
        sent: Whatever the registered sender returned.

    Returns:
        ``(acknowledged_count, platform_message_ids)``. The id list can be
        shorter than the count if the platform returned an object whose id this
        cannot find; the count, not the ids, decides delivery.
    """
    if not sent:
        return 0, []
    try:
        messages = list(sent)
    except TypeError:  # a single message object, not a sequence
        messages = [sent]

    count = 0
    message_ids: list[str] = []
    for msg in messages:
        if msg is None:
            continue
        count += 1
        mid = _platform_id(msg)
        if mid is not None:
            message_ids.append(str(mid))
    return count, message_ids


#: Where each platform keeps the id of a message it just sent, in the order we
#: look. Telegram is ``message_id``; Slack is ``ts``, and a ``SlackResponse`` is
#: a mapping rather than an object, so both access shapes are tried. Without
#: this, a Slack send produced an EMPTY ``platform_ids`` and
#: ``channel_bus.on_post_delivery`` wrote no ``channel_message_map`` rows — a
#: briefing nobody could ever reply to.
_PLATFORM_ID_FIELDS = ("message_id", "ts", "id")


def _platform_id(msg: Any) -> Any | None:
    """The platform's own id for one sent message, or None if it exposes none."""
    for key in _PLATFORM_ID_FIELDS:
        value = getattr(msg, key, None)
        if value is not None:
            return value
    for source in (msg, getattr(msg, "data", None)):
        if isinstance(source, dict):
            for key in _PLATFORM_ID_FIELDS:
                if source.get(key) is not None:
                    return source[key]
    return None


def receipt_from(
    sent: Any,
    expected: int,
    *,
    status: str | None = None,
    target: str = "",
    body: str = "",
    post_delivery: bool = True,
) -> SendReceipt:
    """Build a receipt from a sender's return value.

    Every channel goes through this rather than counting for itself — two
    opinions about what "acknowledged" means is how one surface ends up
    stricter than another about the same evidence.
    """
    acknowledged, platform_ids = acknowledged_messages(sent)
    return SendReceipt(
        acknowledged=acknowledged,
        expected=expected,
        platform_ids=platform_ids,
        status=status,
        target=target,
        body=body,
        post_delivery=post_delivery,
    )


@runtime_checkable
class Channel(Protocol):
    """One surface the instance reaches people over.

    ``name`` is the value a manifest's ``delivery.channel`` has to match, and
    the key the registry stores the channel under.

    ``inbound_router`` is the slot for the receiving half. It is ``None`` for
    every channel in this release: the inbound pipeline still lives in
    ``engine/telegram.py`` and ``engine/slack.py``, which duplicate
    authorize → resolve identity → session key → ``runner.execute`` → reply.
    Unifying those needs a second real inbound surface to generalise against,
    so the slot is declared and left empty rather than filled with a
    single-caller abstraction.
    """

    name: str
    inbound_router: Any | None

    async def start(self) -> None:
        """Begin receiving. A send-only channel may do nothing."""
        ...

    async def stop(self) -> None:
        """Stop receiving and release transport resources."""
        ...

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        """Send ``text`` to ``target`` and return what can be proved about it.

        Must not raise for an ordinary delivery failure: a failure is a receipt
        with ``acknowledged == 0``, because a caller that has to catch an
        exception to notice non-delivery will eventually forget to.
        """
        ...

    async def health(self) -> dict[str, Any]:
        """Whatever the operator needs to see about this channel's readiness."""
        ...

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Put a question to the person at ``target`` and wait for an answer.

        ``target`` is *where* — the address the question is sent to.
        ``addressee`` is *who* — the channel-native id of the person being
        asked, and a channel that can receive must bind its pending question to
        both and settle it only for an answer matching both. An empty
        ``addressee`` means "whoever the platform's own authorization says may
        answer here", which for Telegram is the operator. Getting this wrong is
        how an answer typed by one person settles a question asked of another.

        ``options`` offers a fixed set of choices; empty means free text. The
        return is the answer, or ``None`` for "nobody answered" — a timeout, an
        unreachable surface, or nobody to ask at all.

        ``None`` is the **only** non-answer. A channel must never return one of
        ``options`` because the clock ran out: that is an approval nobody gave,
        and it is the failure this whole module is written against.
        ``NotImplementedError`` is likewise a legitimate outcome and not a bug
        — a sink has nobody to ask — so **every caller must catch it** and fall
        back to whatever it does when no person is reachable.
        """
        ...

    async def resolve_identity(self, native_id: str) -> Any:
        """Map a platform-native sender id onto a Genus identity, or ``None``.

        **One path, one cache.** The Telegram and Slack implementations both
        delegate to :func:`robothor.identity.resolvers.resolve_identity`, and so
        does the access gate (``channels/access.py::_resolve_known``) — which
        calls that function directly rather than going through the registry to
        find this method. So this method currently has no production caller, and
        that is a deliberate accepted state rather than an oversight: routing the
        gate through the registry would put a lookup on the inbound hot path and
        introduce a new failure mode (a channel that raises
        ``NotImplementedError``) into a function whose contract is "never
        raises", in exchange for nothing — there would still be exactly one
        resolution path and one cache. It earns a caller when the inbound
        pipeline itself moves behind :attr:`inbound_router`.

        Declared with the one argument every implementation needs. Telegram and
        Slack additionally accept a keyword ``tenant_id``, which widens what
        they take rather than narrowing it -- a channel that only accepts
        ``native_id`` still satisfies this, and ``EventBusChannel`` does.

        ``None`` means *nobody is bound to this id*, which is a fact and not a
        failure: it is what ``robothor/engine/channels/access.py`` acts on to
        decide whether the sender is paired, allowlisted or refused. A channel
        must never answer with a fabricated identity — the Telegram ladder's
        default-chat ``owner`` fallback is the standing example of why, and it
        is gated behind a flag for exactly that reason.
        """
        ...
