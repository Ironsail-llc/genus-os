"""Which channel a name resolves to — and which ones a mere install cannot arm.

Two kinds of channel live here.

**Built-ins** (``telegram``, ``event_bus``, ``slack``, ``webchat``) are
registered lazily on the first lookup, so ``get_channel("telegram")`` answers
correctly in a daemon, a test, or a one-off script without anyone having to
remember to wire them.

**Plugin channels** (``genus.channels``) are merged from
:func:`robothor.plugins.load_plugins`, and are **inert until the operator names
them** in ``ROBOTHOR_CHANNELS``. That is the ``genus.sandboxes`` rule, adopted
here for the same reason: every other plugin seam takes effect the moment a
package is installed, which is right for a tool or a model and wrong for the
thing the operator's output is handed to. A package able to become the delivery
surface merely by being present could quietly intercept every briefing, and
nothing would look different.

Naming a channel that is not installed does not fall back to Telegram. The
lookup returns ``None`` and ``deliver()`` records
``failed:no_channel:<name>`` — loud, and visible in the same column analytics
and the heartbeat status ping already read. A silent fall-back would turn a
misconfiguration into an invisible redirect, which is the failure this seam
exists to prevent.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.channels.base import Channel

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_CHANNELS",
    "CHANNELS_ENV",
    "WARM_TIMEOUT_S",
    "enabled_plugin_channels",
    "get_channel",
    "list_channels",
    "register_channel",
    "reset_channels",
    "warm_channels",
]

#: Channel names the platform owns. A plugin offering one of these is refused
#: by the loader (they are passed as ``reserved_names``), and
#: :func:`register_channel` refuses to overwrite one.
#:
#: ``slack`` is one of them even on an instance that has never configured it.
#: It has to be: ``SlackBot.start()`` registers a ``slack`` platform sender, and
#: a non-built-in name gets a :class:`~robothor.engine.channels.sender.
#: SenderChannel` wrapped around it — which would replace the real channel with
#: a shim that can only send once the *inbound* socket bot has started. The
#: built-in exemption in ``delivery._register_sender_channel`` is what keeps the
#: two halves independent, and it reserves the name against plugins for free.
#:
#: ``webchat`` is one of them for a stronger reason than Slack's: the Helm is
#: part of the platform rather than an integration, and the channel writes into
#: the member's own ``chat_sessions`` rows and inbox. A plugin able to claim the
#: name could redirect every member-facing delivery into a surface of its own
#: choosing, and the session it wrote to would still look like the member's.
BUILTIN_CHANNELS = frozenset({"telegram", "event_bus", "slack", "webchat"})

#: How long plugin discovery may take before the daemon stops waiting for it.
#: ``warm_channels`` sits between "all subsystems started" and ``READY=1``, and
#: it runs third-party ``ep.load()`` imports: without a budget one package that
#: blocks on a network call at import time holds up systemd's readiness
#: notification indefinitely, and the unit is killed for a timeout that names
#: the engine rather than the plugin. Exceeding it is a warning and a lazy
#: resolution later, not a failure — the cache simply stays cold.
WARM_TIMEOUT_S = 10.0

#: Environment variable naming the plugin channels the operator has armed.
#: Provisional: it becomes a persisted list once there is a command that adds a
#: channel to the instance's configuration.
CHANNELS_ENV = "ROBOTHOR_CHANNELS"

#: Built-ins and sender shims. Registered explicitly, never expire.
_channels: dict[str, Channel] = {}

_builtins_registered = False

#: Re-entrant so a built-in whose import reaches back into the engine cannot
#: deadlock its own registration. Held while the built-ins are registered so no
#: other thread can observe the half-built registry and record
#: ``failed:no_channel:telegram`` for a correctly configured agent.
_builtins_lock = threading.RLock()

#: ``(plugin generation, channels)``. Rebuilt when the generation moves, which
#: is what makes ``reload_plugins()`` visible here without anyone tracking this
#: cache.
_plugin_cache: tuple[int, dict[str, Any]] | None = None

#: ``name -> (plugin generation, channel)`` for channels BUILT from a plugin
#: spec. Deliberately separate from ``_channels``: a plugin channel must be
#: re-checked against the armed set and the plugin generation on every lookup,
#: or the opt-in gate becomes one-way (removing a name from ROBOTHOR_CHANNELS
#: would not disarm it) and a reloaded distribution would keep serving
#: deliveries from the object installed before the reload.
_plugin_channels_built: dict[str, tuple[int, Channel]] = {}


def register_channel(name: str, channel: Channel, *, builtin: bool = False) -> None:
    """Register ``channel`` under ``name``.

    Args:
        name: the name a manifest's ``delivery.channel`` must match.
        channel: the implementation.
        builtin: True only for the platform's own channels. Anything else
            claiming a name in :data:`BUILTIN_CHANNELS` is refused: a
            registration that could displace the Telegram wrapper would be a
            takeover of the operator's own surface, not an extension.

    Raises:
        ValueError: if ``name`` is empty, or names a built-in and
            ``builtin`` is False.
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("a channel name may not be empty")
    if not builtin and clean in BUILTIN_CHANNELS:
        raise ValueError(
            f"{clean!r} is a built-in channel and may not be replaced — "
            "register under a different name"
        )
    _channels[clean] = channel
    logger.info("Registered channel: %s", clean)


def _ensure_builtins() -> None:
    """Register the platform's own channels once, on first use.

    The flag is set only AFTER both registrations land. Setting it first would
    publish an empty registry for as long as the two imports take, and any
    lookup landing in that window would record ``failed:no_channel:telegram``
    for a perfectly configured agent — the registry is first touched on the
    first delivery, not at boot, so that window is real.
    """
    global _builtins_registered
    if _builtins_registered:
        return
    with _builtins_lock:
        if _builtins_registered:
            return
        try:
            from robothor.engine.channels.event_bus import EventBusChannel
            from robothor.engine.channels.slack import SlackChannel
            from robothor.engine.channels.telegram import TelegramChannel
            from robothor.engine.channels.webchat import WebchatChannel

            register_channel("telegram", TelegramChannel(), builtin=True)
            register_channel("event_bus", EventBusChannel(), builtin=True)
            # Always configured: the Helm is part of the platform, so there is
            # no credential for an instance to be missing.
            register_channel("webchat", WebchatChannel(), builtin=True)
            # Registered configured or not. An instance with no Slack token
            # gets `failed:slack_not_configured` from the send, which names the
            # missing credential; leaving the name unresolvable would report
            # `failed:no_channel:slack` and send the operator looking for a
            # platform feature that is right here.
            register_channel("slack", SlackChannel(), builtin=True)

            # Sender shims too: a registration happens once, at bot start, so
            # a registry that forgot them would never get them back.
            from robothor.engine.delivery import rebuild_sender_channels

            rebuild_sender_channels()
        except Exception as exc:  # pragma: no cover - an import cycle would show here
            logger.error("Built-in channels failed to register: %s", exc)
            return
        _builtins_registered = True


def enabled_plugin_channels() -> frozenset[str]:
    """The plugin channel names the operator has armed.

    Read through ``get_settings()`` so the name appears in
    ``docs/reference/configuration.md`` and in ``genus config`` rather than
    being a string only this module knows about.
    """
    try:
        from robothor.settings import get_settings

        raw = get_settings().channels.enabled or ""
    except Exception as exc:  # noqa: BLE001 — unrelated bad config must not
        # break a lookup for a built-in channel. Nothing is armed, so a named
        # plugin channel becomes failed:no_channel — loud, not a redirect.
        logger.warning("Could not resolve %s, no plugin channel is armed: %s", CHANNELS_ENV, exc)
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _plugin_channels() -> dict[str, Any]:
    """Channels contributed by installed plugins, cached per plugin generation."""
    global _plugin_cache
    from robothor.plugins import generation, load_plugins

    current = generation()
    if _plugin_cache is not None and _plugin_cache[0] == current:
        return _plugin_cache[1]

    contributed: dict[str, Any] = {}
    try:
        loaded = load_plugins(reserved_names=set(BUILTIN_CHANNELS))
        contributed = dict(loaded.channels or {})
        for failure in loaded.failures:
            if failure.group == "genus.channels":
                logger.warning("Channel plugin %r refused: %s", failure.name, failure.reason)
    except Exception as exc:  # noqa: BLE001 — a broken plugin must not stop delivery
        logger.warning("Channel plugin discovery failed: %s", exc)
    _plugin_cache = (current, contributed)
    return contributed


def _build(spec: Any) -> Channel | None:
    """Turn a plugin's contribution into a channel.

    A plugin may contribute the channel itself or a zero-argument factory for
    it; anything without a ``send`` is refused rather than registered and hoped
    for.
    """
    candidate = spec
    if not hasattr(candidate, "send") and callable(candidate):
        try:
            candidate = candidate()
        except Exception as exc:  # noqa: BLE001
            logger.error("Channel factory raised: %s", exc)
            return None
    if not hasattr(candidate, "send"):
        logger.error("Channel %r provides no send(); refusing to use it", spec)
        return None
    return cast("Channel", candidate)


def get_channel(name: str) -> Channel | None:
    """The channel ``name`` resolves to, or ``None``.

    ``None`` is a real answer, not an error: the caller records
    ``failed:no_channel:<name>`` so a misconfigured manifest is visible in
    ``agent_runs`` instead of being redirected somewhere that happens to work.

    A plugin channel is re-checked against the armed set on every lookup, so
    taking a name out of ``ROBOTHOR_CHANNELS`` disarms it at the next settings
    resolution — an opt-in gate that only ever opens is not a gate. "Next
    resolution", not "immediately": ``get_settings()`` is process-cached, so an
    env change reaches a running engine only after ``reset_settings()`` or the
    ``robothor-engine`` restart ``ChannelSettings.restart_units`` declares.
    """
    clean = (name or "").strip()
    if not clean:
        return None
    _ensure_builtins()
    existing = _channels.get(clean)
    if existing is not None:
        return existing
    if clean not in enabled_plugin_channels():
        return None

    from robothor.plugins import generation

    current = generation()
    built = _plugin_channels_built.get(clean)
    if built is not None and built[0] == current:
        return built[1]

    spec = _plugin_channels().get(clean)
    if spec is None:
        logger.warning(
            "Channel %r is named in %s but no installed plugin provides it", clean, CHANNELS_ENV
        )
        _plugin_channels_built.pop(clean, None)
        return None
    channel = _build(spec)
    if channel is None:
        _plugin_channels_built.pop(clean, None)
        return None
    _plugin_channels_built[clean] = (current, channel)
    return channel


def list_channels() -> dict[str, Channel]:
    """Every channel a delivery could currently resolve to.

    Built-ins plus registered senders plus the armed plugin channels — never
    the installed-but-unnamed ones, because listing them as available is what
    would make an operator believe a manifest could use one.
    """
    _ensure_builtins()
    out: dict[str, Channel] = dict(_channels)
    for armed in enabled_plugin_channels():
        if armed in out:
            continue
        channel = get_channel(armed)
        if channel is not None:
            out[armed] = channel
    return out


async def warm_channels() -> None:
    """Resolve the built-ins and plugin discovery before anything delivers.

    ``_plugin_channels()`` runs ``entry_points()`` and ``ep.load()`` — which
    *imports third-party modules* — and ``get_channel`` is called from inside
    ``deliver()``, an async function. Left to resolve lazily, the first
    delivery to name a plugin channel would block the event loop on a package
    import while a briefing was going out. Called once at daemon start, the
    discovery happens off the delivery path and the per-generation cache serves
    every later lookup.

    Never raises, and never waits forever: a broken distribution must not stop
    the engine booting, and a SLOW one must not hold up ``READY=1``. The
    failure is already reported by ``_plugin_channels``; a timeout leaves the
    cache cold, so the discovery happens on the first lookup that needs it.
    """
    import asyncio

    _ensure_builtins()
    try:
        await asyncio.wait_for(asyncio.to_thread(_plugin_channels), timeout=WARM_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "Channel plugin discovery did not finish within %.0fs; carrying on without a "
            "warmed cache. A plugin is blocking at import time.",
            WARM_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — boot must survive a bad plugin
        logger.warning("Channel discovery failed while warming: %s", exc)


def reset_channels() -> None:
    """Drop every registration and cached lookup. For tests and reloads.

    Does NOT clear ``delivery._platform_senders``: a sender registration is a
    fact about the running process, and ``_ensure_builtins`` rebuilds a channel
    for each one on the next lookup.
    """
    global _builtins_registered, _plugin_cache
    with _builtins_lock:
        _channels.clear()
        _plugin_channels_built.clear()
        _builtins_registered = False
        _plugin_cache = None
