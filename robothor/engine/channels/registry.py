"""Which channel a name resolves to — and which ones a mere install cannot arm.

Two kinds of channel live here.

**Built-ins** (``telegram``, ``event_bus``) are registered lazily on the first
lookup, so ``get_channel("telegram")`` answers correctly in a daemon, a test, or
a one-off script without anyone having to remember to wire them.

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
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.channels.base import Channel

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_CHANNELS",
    "CHANNELS_ENV",
    "enabled_plugin_channels",
    "get_channel",
    "list_channels",
    "register_channel",
    "reset_channels",
]

#: Channel names the platform owns. A plugin offering one of these is refused
#: by the loader (they are passed as ``reserved_names``), and
#: :func:`register_channel` refuses to overwrite one.
BUILTIN_CHANNELS = frozenset({"telegram", "event_bus"})

#: Environment variable naming the plugin channels the operator has armed.
#: Provisional: it becomes a persisted list once there is a command that adds a
#: channel to the instance's configuration.
CHANNELS_ENV = "ROBOTHOR_CHANNELS"

_channels: dict[str, Channel] = {}
_builtins_registered = False

#: ``(plugin generation, channels)``. Rebuilt when the generation moves, which
#: is what makes ``reload_plugins()`` visible here without anyone tracking this
#: cache.
_plugin_cache: tuple[int, dict[str, Any]] | None = None


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
    """Register the platform's own channels once, on first use."""
    global _builtins_registered
    if _builtins_registered:
        return
    # Set the flag first: the imports below reach back into the engine, and a
    # re-entrant lookup must not register twice.
    _builtins_registered = True
    try:
        from robothor.engine.channels.event_bus import EventBusChannel
        from robothor.engine.channels.telegram import TelegramChannel

        register_channel("telegram", TelegramChannel(), builtin=True)
        register_channel("event_bus", EventBusChannel(), builtin=True)
    except Exception as exc:  # pragma: no cover - an import cycle would show here
        _builtins_registered = False
        logger.error("Built-in channels failed to register: %s", exc)


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
    spec = _plugin_channels().get(clean)
    if spec is None:
        logger.warning(
            "Channel %r is named in %s but no installed plugin provides it", clean, CHANNELS_ENV
        )
        return None
    channel = _build(spec)
    if channel is None:
        return None
    _channels[clean] = channel
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


def reset_channels() -> None:
    """Drop every registration and cached lookup. For tests and reloads."""
    global _builtins_registered, _plugin_cache
    _channels.clear()
    _builtins_registered = False
    _plugin_cache = None
