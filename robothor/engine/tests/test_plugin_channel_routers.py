"""A channel that receives needs a door, and the door has to be nailed shut.

``Channel.inbound_router`` has been a declared-and-empty slot since C3 — the
kind of extension point this platform's own base module calls "our most frequent
defect". A plugin channel with a webhook (Teams, and every Bot-Framework-shaped
surface after it) has nowhere to be reached without it, so this fills it: the
engine's own FastAPI app mounts the router of each **armed** plugin channel.

Two rules, and the second is the one an attacker cares about.

*Installed is not armed.* Same rule as ``get_channel``: a package that became a
delivery surface by being installed could intercept every briefing. A package
that could mount an HTTP route by being installed is worse — it does not even
need the operator to name it in a manifest.

*A channel's router may only claim its own path.* ``/api/channels/<name>/…``,
nothing else. Without that check a plugin could publish a router over
``/api/admin/channels`` and answer the operator's own console, which is a
takeover wearing an extension's clothes. The refusal is all-or-nothing: a router
with one out-of-bounds route mounts none of its routes.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.channels import reset_channels
from robothor.engine.channels.routers import mount_plugin_channel_routers
from robothor.plugins import reload_plugins


class _App:
    """Just enough FastAPI to record what was mounted."""

    def __init__(self) -> None:
        self.mounted: list[Any] = []

    def include_router(self, router: Any, **_kw: Any) -> None:
        self.mounted.append(router)


class _Route:
    def __init__(self, path: str) -> None:
        self.path = path


class _Router:
    def __init__(self, *paths: str) -> None:
        self.routes = [_Route(p) for p in paths]


class _PluginChannel:
    def __init__(self, name: str = "teams", router: Any = None) -> None:
        self.name = name
        self.inbound_router = router

    async def send(self, target: str, text: str, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _ChannelEP:
    group = "genus.channels"

    def __init__(self, channels: dict, name: str = "testchan") -> None:
        self.name = name
        self._channels = channels

    def load(self) -> dict:
        return {"genus_contract_version": "1.0", "channels": self._channels}


@pytest.fixture
def install(monkeypatch):
    def _install(channels: dict) -> None:
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_ChannelEP(channels)])
        reload_plugins()
        reset_channels()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()
    reset_channels()


@pytest.fixture(autouse=True)
def _nothing_armed(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
    reset_settings()
    reset_channels()
    yield
    reset_settings()
    reset_channels()


def _arm(monkeypatch, value: str) -> None:
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_CHANNELS", value)
    reset_settings()
    reset_channels()


class TestInstallingIsNotMounting:
    def test_an_installed_but_unnamed_channel_mounts_nothing(self, install, monkeypatch):
        router = _Router("/api/channels/teams/messages")
        install({"teams": _PluginChannel(router=router)})
        app = _App()
        mount_plugin_channel_routers(app)
        assert app.mounted == [], "a package published an HTTP route by being installed"

    def test_an_armed_channel_is_mounted(self, install, monkeypatch):
        router = _Router("/api/channels/teams/messages")
        install({"teams": _PluginChannel(router=router)})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app)
        assert app.mounted == [router]

    def test_a_channel_with_no_router_is_not_an_error(self, install, monkeypatch):
        install({"teams": _PluginChannel(router=None)})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app)
        assert app.mounted == []


class TestARouterMayOnlyClaimItsOwnPath:
    def test_a_router_reaching_outside_its_channel_path_is_refused(self, install, monkeypatch):
        """The whole router, not just the offending route: a package that
        reached for the operator's console has shown what it is willing to do."""
        router = _Router("/api/channels/teams/messages", "/api/admin/channels")
        install({"teams": _PluginChannel(router=router)})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app)
        assert app.mounted == []

    def test_a_router_claiming_another_channels_path_is_refused(self, install, monkeypatch):
        router = _Router("/api/channels/slack/messages")
        install({"teams": _PluginChannel(router=router)})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app)
        assert app.mounted == []

    def test_a_route_that_merely_starts_with_the_prefix_string_is_refused(
        self, install, monkeypatch
    ):
        """``/api/channels/teamsX`` shares a prefix with ``/api/channels/teams``
        and is a different path. A startswith check without the separator is how
        a namespace gets escaped."""
        router = _Router("/api/channels/teamsX/messages")
        install({"teams": _PluginChannel(router=router)})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app)
        assert app.mounted == []


class TestAReceivingChannelIsHandedTheRuntime:
    """A plugin router has no way to reach the runner: the engine builds it, and
    `init_chat`-style wiring is a platform function a package cannot call. So a
    channel that declares ``bind_runtime`` is handed the runner and the engine
    config at mount time — the same handshake ``init_chat`` performs for the
    built-in chat router, through a slot rather than an import."""

    def test_a_channel_that_declares_bind_runtime_is_given_the_runner(
        self, install, monkeypatch
    ):
        bound: dict[str, Any] = {}

        class _Bindable(_PluginChannel):
            def bind_runtime(self, *, runner: Any, config: Any) -> None:
                bound["runner"] = runner
                bound["config"] = config

        runner, config = object(), object()
        install({"teams": _Bindable(router=_Router("/api/channels/teams/messages"))})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app, runner=runner, config=config)

        assert bound["runner"] is runner
        assert bound["config"] is config
        assert len(app.mounted) == 1

    def test_a_channel_that_cannot_be_bound_is_not_mounted(self, install, monkeypatch):
        """Fail closed: an endpoint with no runner would answer 200 to every
        activity and drop it, which looks exactly like a working install."""

        class _Unbindable(_PluginChannel):
            def bind_runtime(self, *, runner: Any, config: Any) -> None:
                raise RuntimeError("the plugin refused the runtime")

        install({"teams": _Unbindable(router=_Router("/api/channels/teams/messages"))})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app, runner=object(), config=object())
        assert app.mounted == []


class TestABrokenPluginMustNotStopBoot:
    def test_a_channel_whose_router_raises_is_skipped(self, install, monkeypatch):
        class _Exploding:
            name = "teams"

            @property
            def inbound_router(self) -> Any:
                raise RuntimeError("a broken plugin")

            async def send(self, target: str, text: str, **kw: Any) -> Any:  # pragma: no cover
                raise NotImplementedError

        install({"teams": _Exploding()})
        _arm(monkeypatch, "teams")
        app = _App()
        mount_plugin_channel_routers(app)  # must not raise
        assert app.mounted == []

    def test_an_app_that_rejects_the_router_does_not_take_the_engine_down(
        self, install, monkeypatch
    ):
        class _RefusingApp(_App):
            def include_router(self, router: Any, **_kw: Any) -> None:
                raise RuntimeError("FastAPI refused it")

        install({"teams": _PluginChannel(router=_Router("/api/channels/teams/messages"))})
        _arm(monkeypatch, "teams")
        mount_plugin_channel_routers(_RefusingApp())


class TestTheEngineActuallyMountsThem:
    def test_the_health_app_mounts_plugin_channel_routers(self):
        """AST, not a substring: a declared-and-inert extension point is this
        platform's most frequent defect, and this one has been inert since C3."""
        from robothor.engine.tests.astcheck import called_names, function_def

        branch = function_def("robothor.engine.health", "_mount_subsystem_routers")
        assert "mount_plugin_channel_routers" in called_names(branch)
