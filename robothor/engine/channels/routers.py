"""Mounting the receiving half of a plugin channel — the one slot C3 left empty.

``Channel.inbound_router`` was declared in :mod:`robothor.engine.channels.base`
and filled by nothing, which that module itself calls out as "a
declared-and-inert extension point … this platform's most frequent defect". A
channel that receives over HTTP (a Bot Framework messaging endpoint; a webhook
of any shape) has nowhere to be reached until something mounts it, and a plugin
cannot reach into the engine's app to mount itself.

Two rules govern what gets mounted, and the second matters more than it looks.

**Installed is not armed.** Exactly the gate ``registry.get_channel`` applies,
for a stronger reason: a package that becomes a *delivery* surface by being
installed can intercept a briefing, and a package that publishes an *HTTP route*
by being installed does not need the operator to name it anywhere at all.

**A channel's router may only claim its own path.** ``/api/channels/<name>``, or
something below it. Nothing else is mounted, and a router with one out-of-bounds
route mounts none of its routes — all-or-nothing, the same shape the plugin
loader applies to a package that reaches for a reserved service name. Without
this, a plugin could publish a route over ``/api/admin/channels`` and answer the
operator's own console; the engine's app has no other owner-based check to fall
back on, because every route on it is the platform's by construction.

The AUTHENTICATION of what arrives on that route is the channel's own business
and deliberately not attempted here. The engine cannot know what proves a Teams
activity genuine, and a shared "is this request ok?" hook would be a gate with
one caller pretending to be a policy. What this module guarantees is narrower
and checkable: only an armed channel is reachable, and only at its own address.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNEL_PATH_PREFIX",
    "ChannelRuntime",
    "channel_path_prefix",
    "is_mounted",
    "mount_plugin_channel_routers",
    "mounted_channels",
    "reset_mounted",
]

#: Every plugin channel's HTTP surface lives under this, namespaced by channel.
CHANNEL_PATH_PREFIX = "/api/channels"

#: Channels whose router is actually ON an app right now.
#:
#: Recorded because "mounted" is not the same claim as "the router object was
#: built", and the doctor check that exists to catch "armed nowhere" was reading
#: the second and printing the first — so it stayed green when the path-claim
#: check refused the router, when ``include_router`` raised, and when the
#: channel was skipped for having no runner. A check that cannot see the failure
#: one layer above the one it watches is the shape of every inert control this
#: platform has shipped.
_mounted: set[str] = set()


def mounted_channels() -> frozenset[str]:
    """The plugin channels whose inbound router is mounted in this process."""
    return frozenset(_mounted)


def is_mounted(name: str) -> bool:
    """Whether ``name``'s inbound router is mounted. The honest version of
    "is there an endpoint" — ask the mounting, not the object."""
    return name in _mounted


def reset_mounted() -> None:
    """Forget what is mounted. For tests, and for an app rebuilt in-process."""
    _mounted.clear()


@dataclass(frozen=True)
class ChannelRuntime:
    """What a receiving channel is handed, and deliberately no more.

    The first cut passed ``EngineConfig``, which carries the operator's Telegram
    bot token and their default chat id. A plugin is imported into the daemon and
    could reach those anyway — this crosses no boundary that exists — but a slot
    the platform *declares* should hand over the narrowest thing that works, or
    the declaration teaches the wrong lesson to the next channel that uses it.

    Grows a field when a channel needs one, and not before.
    """

    tenant_id: str = DEFAULT_TENANT


def channel_path_prefix(name: str) -> str:
    """The only path prefix ``name``'s router may publish under."""
    return f"{CHANNEL_PATH_PREFIX}/{name}"


def _claims_only_its_own_path(router: Any, name: str) -> bool:
    """Whether every route on ``router`` lives under this channel's prefix.

    The separator is part of the test: ``/api/channels/teamsX`` starts with
    ``/api/channels/teams`` and is a different channel's namespace, which is
    how a prefix check that forgets the boundary gets escaped.
    """
    prefix = channel_path_prefix(name)
    routes = list(getattr(router, "routes", []) or [])
    if not routes:
        logger.warning("channel %r contributed a router with no routes; not mounting it", name)
        return False
    for route in routes:
        path = str(getattr(route, "path", "") or "")
        if ".." in path:
            # A traversal segment cannot shadow a platform route (they are
            # registered first and matched in order), so this is hygiene rather
            # than a hole — but a claim check that accepts
            # `/api/channels/teams/../admin/channels` is not checking the thing
            # it says it checks.
            logger.error("channel %r contributed a route containing '..'; refusing it", name)
            return False
        if path != prefix and not path.startswith(prefix + "/"):
            logger.error(
                "channel %r contributed a route outside %s; refusing the whole router",
                name,
                prefix,
            )
            return False
    return True


def _bind_runtime(channel: Any, name: str, runner: Any, runtime: ChannelRuntime) -> bool:
    """Hand a receiving channel the runtime it cannot reach for itself.

    True when the channel took it, or declared no interest. False in two cases,
    and both leave the router unmounted:

    * the channel declared ``bind_runtime`` and the call **raised**;
    * there is **no runner to give it**. ``create_health_app`` legitimately
      builds an app without one (a CLI, a test), and the chat and IDE routers are
      already guarded the same way. A channel handed ``runner=None`` does not
      raise — it stores the ``None`` and then answers 200 to every authenticated
      activity and drops it, which is verbatim the failure this function's
      docstring claimed to prevent.
    """
    bind = getattr(channel, "bind_runtime", None)
    if bind is None:
        return True
    if runner is None:
        logger.warning(
            "Channel %r receives but there is no runner in this process, so its "
            "endpoint is NOT mounted. An endpoint that acknowledged messages and "
            "dropped them would be indistinguishable from a working install.",
            name,
        )
        return False
    try:
        bind(runner=runner, runtime=runtime)
    except Exception as exc:  # noqa: BLE001 — a refusing plugin is not a dead engine
        logger.error(
            "Channel %r refused the runtime, so its endpoint is NOT mounted: %s", name, exc
        )
        return False
    return True


def mount_plugin_channel_routers(
    app: Any, *, runner: Any | None = None, tenant_id: str = DEFAULT_TENANT
) -> list[str]:
    """Mount the inbound router of every armed plugin channel.

    A channel that declares ``bind_runtime(*, runner, runtime)`` is handed the
    runner and a :class:`ChannelRuntime` — the handshake ``init_chat`` performs
    for the built-in chat router, offered as a slot because a package cannot call
    a platform function that takes the engine's own objects, and narrowed to what
    a channel actually needs rather than to the config object that happens to be
    in scope. A channel that refuses the binding, or that there is no runner for,
    is **not mounted**: an endpoint with no runner answers 200 to every activity
    and drops it, which is indistinguishable from a working install.

    Returns the names mounted, for the caller that wants to log or report them.
    Never raises: a broken distribution must not stop the engine booting, which
    is the same contract ``warm_channels`` holds one layer down. A channel that
    is skipped says so in the journal — silence here would be a webhook that
    answers 404 with nothing anywhere explaining why.
    """
    from robothor.engine.channels.registry import enabled_plugin_channels, get_channel

    mounted: list[str] = []
    try:
        armed = sorted(enabled_plugin_channels())
    except Exception as exc:  # noqa: BLE001 — bad config must not stop boot
        logger.warning("Could not resolve the armed channel set: %s", exc)
        return mounted

    for name in armed:
        try:
            channel = get_channel(name)
            router = getattr(channel, "inbound_router", None) if channel is not None else None
        except Exception as exc:  # noqa: BLE001 — a plugin's own property may raise
            logger.error("Channel %r could not be asked for its router: %s", name, exc)
            continue
        if router is None:
            continue
        if not _claims_only_its_own_path(router, name):
            _mounted.discard(name)
            continue
        if not _bind_runtime(channel, name, runner, ChannelRuntime(tenant_id=tenant_id)):
            _mounted.discard(name)
            continue
        try:
            app.include_router(router)
        except Exception as exc:  # noqa: BLE001 — one bad router is not a dead engine
            logger.error("Channel %r router could not be mounted: %s", name, exc)
            _mounted.discard(name)
            continue
        mounted.append(name)
        # Recorded only HERE, after the include actually returned. Everything
        # that asks "is there an endpoint" reads this rather than asking a
        # channel whether it built a router object.
        _mounted.add(name)
        logger.info(
            "Mounted the inbound router for channel %r at %s", name, channel_path_prefix(name)
        )
    return mounted
