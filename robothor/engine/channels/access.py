"""One gate every inbound channel asks before a message becomes a run.

There were two gates before this, and neither was a policy an operator could
state. Telegram's was a ladder inside ``_resolve_user`` that fabricated an
``owner`` identity for the default chat, a ``user`` for a group, and refused a
private stranger with a sentence. Slack's was ``_authorized``, which returned
**True when neither allowlist was configured** — so an instance that had merely
been pointed at a workspace let any member of it drive the main agent, with a
startup warning as the only compensating control. A third channel would have
had to invent a third answer, and the answer nobody would have noticed is the
one that says yes.

So: three modes, named, and the same three for every channel.

``open``
    Resolve an identity if there is one, and run either way. This is what
    Telegram does today, which is precisely why Telegram *defaults* to it —
    shipping this gate must not be a behaviour change for the one instance
    surface that already works.
``allowlist``
    The channel supplies a membership test and this gate calls it. Slack's
    user-OR-channel truth table survives unchanged.
``pairing``
    An unknown sender gets a six-character code and nothing else. They cannot
    reach the runner, and — the invariant the whole design turns on — nothing
    they can send afterwards can approve it. See
    :mod:`robothor.engine.channels.identities`.

A known identity short-circuits every mode, checked before the mode is even
read. An operator turning a channel to ``pairing`` is deciding about strangers,
not re-deciding about the people already bound to it, and a gate that made them
re-pair would be one whose safe setting is the one nobody dares turn on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from robothor.constants import DEFAULT_TENANT
from robothor.engine.channels import identities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

__all__ = [
    "DIRECT_SURFACE",
    "GROUP_SURFACE",
    "MODES",
    "PAIRING_CODE_ALPHABET",
    "PAIRING_CODE_LENGTH",
    "AccessDecision",
    "AccessMode",
    "PAIRING_REPLY_TEMPLATE",
    "access_mode",
    "evaluate",
    "mode_was_configured",
    "pairing_reply",
    "reset_reply_budget",
]

AccessMode = Literal["pairing", "allowlist", "open"]

MODES: tuple[str, ...] = ("pairing", "allowlist", "open")

#: The mode an unrecognised setting value resolves to. Fail closed: a typo in
#: ``ROBOTHOR_SLACK_ACCESS`` must not be the thing that opens a surface.
FALLBACK_MODE = "pairing"

#: A 1:1 conversation — a Telegram private chat, a Slack DM. The only surface a
#: code is ever minted on.
DIRECT_SURFACE = "direct"

#: A shared conversation. An unknown sender here is ignored, counted and not
#: answered: a code posted in a room is a code anyone in the room can carry to
#: the operator, which is the whole of the attack pairing exists to stop.
GROUP_SURFACE = "group"

#: Re-exported so a caller validating a code does not have to import the DAL.
PAIRING_CODE_ALPHABET = identities.PAIRING_CODE_ALPHABET
PAIRING_CODE_LENGTH = identities.PAIRING_CODE_LENGTH

#: The whole of what an unknown sender is told. One sentence, because a bare
#: six-character code tells a stranger nothing about what to do with it and the
#: first cut sent exactly that.
#:
#: What it must NOT contain is the point: no operator name, no instance or brand
#: name, no address, no "ask <someone>". A stranger who guessed a bot's handle
#: learns only that pairing exists here -- not who runs it, not what it is, and
#: nothing that helps them find a human to social-engineer. "the operator of
#: this assistant" is deliberately a role and not an identity.
PAIRING_REPLY_TEMPLATE = "Share this code with the operator of this assistant to be paired: {code}"

#: At most three replies per sender per hour, the shape
#: ``_ONBOARDING_NOTIFY_INTERVAL_SECONDS`` already uses in ``engine/telegram.py``.
#: The code itself is idempotent inside its TTL, so this is not about minting —
#: it is about not letting somebody with a script make the bot answer forever.
REPLY_WINDOW_SECONDS = 3600.0
MAX_REPLIES_PER_WINDOW = 3

#: How long a resolution MISS is remembered while a channel is in ``pairing``.
#:
#: The resolver's own negative TTL is 60s, which is the wrong answer for a
#: sender waiting to be approved: the operator approves them, their next message
#: is still refused, and it mints a SECOND pending code that arrives in the
#: operator's list as a second request from the same person. Five seconds keeps
#: the burst protection and makes an approval feel like it took effect. It costs
#: one query per unknown sender per five seconds, bounded by the reply budget.
PENDING_MISS_TTL_SECONDS = 5.0

_replies: dict[tuple[str, str], list[float]] = {}

#: Settings field names are identifiers; a channel name arrives from a manifest
#: or a plugin. Anything that is not a plain name gets the shared default
#: rather than being interpolated into a ``getattr``.
_CHANNEL_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


@dataclass(frozen=True)
class AccessDecision:
    """What the channel must do with this message.

    ``refusal`` is the exact text to send back, and ``""`` means **send
    nothing** — a suppressed reply and a silently-ignored group message are
    both legitimate outcomes, and a channel that invented a message for them
    would be telling a stranger that somebody is listening.
    """

    allowed: bool
    identity: IdentityContext | None = None
    refusal: str = ""
    pairing_code: str | None = None


def access_mode(channel: str) -> str:
    """The mode in force for ``channel``.

    Read through ``get_settings()``, never ``os.environ``: the env-read ratchet
    in ``tests/test_settings_registry.py`` counts raw reads, and a setting that
    is only reachable by knowing an environment variable name is a setting
    ``genus config`` cannot show, set or document.

    A channel with no dedicated field — every plugin channel — takes
    ``channel_access_default``.
    """
    from robothor.settings import get_settings

    channels = get_settings().channels
    field = _mode_field(channel)
    raw = str(getattr(channels, field, "") or "")
    if not raw.strip():
        # Blank is "unset", and unset falls back to the FIELD's own declared
        # default -- not to ``channel_access_default``. Telegram declares
        # ``open`` for compatibility, and an operator who blanked
        # ``ROBOTHOR_TELEGRAM_ACCESS`` has not thereby asked for ``pairing``;
        # they have asked for whatever the platform ships. Reading the default
        # off the model keeps the two answers from drifting apart.
        raw = str(_DECLARED_DEFAULTS.get(field, "") or "")

    mode = raw.strip().lower()
    if mode not in MODES:
        logger.warning(
            "channel %s has access mode %r, which is not one of %s — falling back to %r",
            channel,
            mode,
            ", ".join(MODES),
            FALLBACK_MODE,
        )
        return FALLBACK_MODE
    return mode


def _mode_field(channel: str) -> str:
    """Which settings field governs ``channel``.

    A channel with a field of its own uses it; everything else -- every plugin
    channel -- uses ``channel_access_default``. Checked against the declared
    field names rather than with a bare ``getattr``, so a channel called
    ``verify_target`` cannot reach a neighbouring setting.
    """
    from robothor.settings.model import ChannelSettings

    if _CHANNEL_NAME.match(channel or ""):
        candidate = f"{channel}_access"
        if candidate in ChannelSettings.model_fields:
            return candidate
    return "channel_access_default"


def _declared_defaults() -> dict[str, str]:
    from robothor.settings.model import ChannelSettings

    return {
        name: str(field.default or "")
        for name, field in ChannelSettings.model_fields.items()
        if name.endswith("_access") or name == "channel_access_default"
    }


#: Resolved once: the model does not change at runtime, and this is on the
#: inbound path of every message.
_DECLARED_DEFAULTS: dict[str, str] = _declared_defaults()


def mode_was_configured(channel: str) -> bool:
    """Whether this channel's mode was CHOSEN, as opposed to defaulted.

    The distinction exists for exactly one caller -- ``SlackBot._access_mode``,
    which keeps an instance that configured a legacy allowlist before modes
    existed on ``allowlist`` rather than locking everyone out of it. That clause
    must apply to the declared default and never to an explicit choice: deciding
    it on the resolved value instead is how an operator who set
    ``ROBOTHOR_SLACK_ACCESS=pairing`` got ``allowlist``, with the warning telling
    them to set the variable they had already set.
    """
    from robothor.settings.provenance import is_configured

    return is_configured(f"channels.{_mode_field(channel)}")


def reset_reply_budget() -> None:
    """Forget every sender's reply budget. A test seam, and what a restart does."""
    _replies.clear()


def _may_reply(channel: str, native_id: str) -> bool:
    now = time.monotonic()
    window = [t for t in _replies.get((channel, native_id), []) if now - t < REPLY_WINDOW_SECONDS]
    if len(window) >= MAX_REPLIES_PER_WINDOW:
        _replies[(channel, native_id)] = window
        return False
    window.append(now)
    _replies[(channel, native_id)] = window
    identities.prune_oldest(_replies)
    return True


def _resolve_known(
    channel: str,
    native_id: str,
    tenant_id: str,
    *,
    negative_ttl_seconds: float | None = None,
) -> IdentityContext | None:
    """The identity bound to this native id, or None.

    Goes through :func:`robothor.identity.resolvers.resolve_identity` — the
    same dispatch ``Channel.resolve_identity`` delegates to — so a channel that
    has an implementation and one that does not resolve identically, and there
    is one cache rather than one per surface. Never raises: that function
    answers ``None`` for an unknown channel, a missing row and a database
    error alike.
    """
    from robothor.identity.resolvers import resolve_identity

    return resolve_identity(
        channel, native_id, tenant_id, negative_ttl_seconds=negative_ttl_seconds
    )


async def evaluate(
    channel: str,
    native_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT,
    display_name: str = "",
    surface: str = DIRECT_SURFACE,
    allowlist: Callable[[], bool] | None = None,
    mode: str | None = None,
) -> AccessDecision:
    """Decide whether this sender's message may become a run.

    ``allowlist`` is the channel's own membership test, called only in
    ``allowlist`` mode. It is a callback rather than a list because the answer
    depends on things this module has no business knowing — for Slack, the
    conversation the message arrived in as well as the sender.

    ``mode`` overrides what :func:`access_mode` would read. Exactly one caller
    passes it — Slack, which resolves its own mode because it carries a
    compatibility clause for instances that configured an allowlist before
    modes existed. Letting it pass the answer in is the difference between one
    decision and two that can disagree: with the mode read twice, the bot's
    ``allowlist`` and the gate's ``pairing`` would both be "correct" and every
    allowlisted sender would be handed a pairing code.

    Nothing here logs a native id or a display name. The line this gate
    replaces did (``engine/slack.py`` logged the raw Slack user id on every
    refusal), which put a workspace's member ids into every log shipper the
    instance has, for the senders with the least reason to trust it.
    """
    resolved_mode = mode if mode in MODES else access_mode(channel)

    # The mode is read BEFORE the identity, and only so a miss can be cached for
    # the right length of time: a sender waiting on a pairing is a miss by
    # definition, and remembering that for a full minute makes an approval look
    # like it did not work. The ORDER OF THE DECISION is unchanged -- a known
    # identity still short-circuits every mode, on the next line.
    identity = await asyncio.to_thread(
        _resolve_known,
        channel,
        native_id,
        tenant_id,
        negative_ttl_seconds=(PENDING_MISS_TTL_SECONDS if resolved_mode == "pairing" else None),
    )
    if identity is not None:
        return AccessDecision(allowed=True, identity=identity)

    if resolved_mode == "open":
        return AccessDecision(allowed=True)

    if resolved_mode == "allowlist":
        listed = False
        if allowlist is not None:
            try:
                listed = bool(allowlist())
            except Exception:
                logger.exception("channel %s allowlist test failed; refusing", channel)
                listed = False
        if not listed:
            logger.info("channel %s refused an unlisted sender", channel)
        return AccessDecision(allowed=listed)

    # ── pairing ──
    if surface == GROUP_SURFACE:
        logger.info("channel %s ignored 1 unknown sender on a group surface", channel)
        return AccessDecision(allowed=False)

    if not _may_reply(channel, native_id):
        logger.info("channel %s suppressed a repeated pairing reply", channel)
        return AccessDecision(allowed=False)

    try:
        code = await asyncio.to_thread(
            identities.mint_code,
            channel=channel,
            native_id=native_id,
            tenant_id=tenant_id,
            display_name=display_name,
        )
    except identities.PairingDeniedError:
        # An expected outcome, not a fault: this sender was refused and the
        # refusal has not expired. Nothing goes back, so a denial is not
        # something a stranger can undo by sending another message.
        logger.info("channel %s ignored a sender whose pairing was denied", channel)
        return AccessDecision(allowed=False)
    except Exception:
        # A stranger must not be able to learn that the database is down, and a
        # message must not be able to take the channel down either.
        logger.exception("channel %s could not mint a pairing code", channel)
        return AccessDecision(allowed=False)

    return AccessDecision(
        allowed=False, refusal=PAIRING_REPLY_TEMPLATE.format(code=code), pairing_code=code
    )


async def pairing_reply(
    channel: str,
    native_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT,
    display_name: str = "",
    surface: str = DIRECT_SURFACE,
) -> str | None:
    """The pairing answer for an unknown sender, or None to fall through.

    The shape Telegram needs. ``None`` means *this gate has nothing to say* —
    the channel is not in ``pairing`` mode, the surface is a group, the reply
    was suppressed, or the mint failed — and the caller should do exactly what
    it did before this gate existed, which for Telegram is its own refusal
    sentence. It is ``None`` rather than ``""`` on purpose:
    ``message.answer("")`` is an API error, so "send nothing of mine" and "send
    your own refusal" have to be distinguishable at the call site.
    """
    mode = access_mode(channel)
    if mode != "pairing":
        return None
    decision = await evaluate(
        channel,
        native_id,
        tenant_id=tenant_id,
        display_name=display_name,
        surface=surface,
        mode=mode,
    )
    return decision.refusal or None
