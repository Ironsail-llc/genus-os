"""A plugin should be able to supply a channel — and installing it must not arm it.

Genus reached people through exactly one surface, and the code said so: the
``ANNOUNCE`` branch of ``delivery.deliver()`` called ``_deliver_telegram``. An
instance wanting Matrix, Discord or a paging provider had to patch the engine.

Like ``genus.sandboxes``, and unlike every other group, this one is **opt-in**.
A channel is what the operator's output goes to; a package able to become the
delivery surface merely by being present could quietly intercept every
briefing, and nothing would look different. So an installed channel is inert
until the operator names it in ``ROBOTHOR_CHANNELS`` — and the test that matters
most below is the one asserting that installation alone changes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.channels import (
    BUILTIN_CHANNELS,
    SendReceipt,
    get_channel,
    list_channels,
    reset_channels,
)
from robothor.plugins import reload_plugins


class _PluginChannel:
    def __init__(self, name: str = "acme") -> None:
        self.name = name
        self.inbound_router = None
        self.sends: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"channel": self.name}

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        self.sends.append(text)
        return SendReceipt(acknowledged=1, expected=1, platform_ids=["1"], target=target)


class _ChannelEP:
    group = "genus.channels"

    def __init__(self, channels: dict, name: str = "testchan") -> None:
        self.name = name
        self._channels = channels

    def load(self) -> dict:
        return {"genus_contract_version": "1.0", "channels": self._channels}


@pytest.fixture
def install(monkeypatch):
    """Install a fake ``genus.channels`` distribution for one test."""

    def _install(channels: dict):
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
def _no_channels_named(monkeypatch):
    """No channel is enabled unless the test says so."""
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
    reset_settings()
    reset_channels()
    yield
    reset_settings()
    reset_channels()


def _enable(monkeypatch, value: str) -> None:
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_CHANNELS", value)
    reset_settings()
    reset_channels()


class TestTheGroupExists:
    def test_the_loader_declares_it(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.channels") == "channels"

    def test_loader_accepts_a_genus_channels_entry_point(self, install):
        from robothor.plugins import load_plugins

        install({"acme": _PluginChannel()})
        loaded = load_plugins(reserved_names=set())
        assert "acme" in (loaded.channels or {})
        assert loaded.failures == []


class TestInstallingIsNotArming:
    def test_installed_channel_is_inert_until_named(self, install, monkeypatch):
        install({"acme": _PluginChannel()})
        assert get_channel("acme") is None, (
            "a package became a delivery surface merely by being installed"
        )

        _enable(monkeypatch, "acme")
        assert get_channel("acme") is not None

    def test_naming_one_channel_does_not_arm_its_neighbour(self, install, monkeypatch):
        install({"acme": _PluginChannel("acme"), "other": _PluginChannel("other")})
        _enable(monkeypatch, "acme")
        assert get_channel("acme") is not None
        assert get_channel("other") is None

    def test_naming_a_channel_that_is_not_installed_is_not_a_silent_telegram(self, monkeypatch):
        """``failed:no_channel:<name>`` is the loud outcome; a fall-back to the
        built-in Telegram channel would hide the misconfiguration entirely."""
        _enable(monkeypatch, "not-installed")
        assert get_channel("not-installed") is None

    def test_a_comma_separated_list_arms_each_name(self, install, monkeypatch):
        install({"acme": _PluginChannel("acme"), "other": _PluginChannel("other")})
        _enable(monkeypatch, "acme, other")
        assert get_channel("acme") is not None
        assert get_channel("other") is not None

    def test_taking_a_name_back_out_disarms_it(self, install, monkeypatch):
        """An opt-in gate that only ever opens is not a gate. The first version
        cached the built channel next to the built-ins, so a resolved plugin
        channel kept serving deliveries after the operator un-armed it."""
        from robothor.settings import reset_settings

        install({"acme": _PluginChannel()})
        _enable(monkeypatch, "acme")
        assert get_channel("acme") is not None

        monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
        reset_settings()
        assert get_channel("acme") is None, "un-arming a channel did not disarm it"
        assert "acme" not in list_channels()

    def test_a_reload_replaces_the_channel_it_serves(self, install, monkeypatch):
        """`reload_plugins()` exists so a capability can be changed without a
        restart. A cached channel object made it a no-op for this group."""
        first = _PluginChannel()
        install({"acme": first})
        _enable(monkeypatch, "acme")
        assert get_channel("acme") is first

        second = _PluginChannel()
        install({"acme": second})
        assert get_channel("acme") is second, "a reloaded distribution kept serving the old object"


class TestTheBuiltinsAreNeverPublishedHalfBuilt:
    def test_a_lookup_never_sees_an_empty_registry(self):
        """The flag used to be set BEFORE the two registrations, so a lookup
        landing in that window recorded `failed:no_channel:telegram` for a
        perfectly configured agent. The registry is first touched on the first
        delivery, not at boot, so the window was real."""
        from robothor.engine.channels import registry

        reset_channels()
        assert registry._builtins_registered is False
        registry._ensure_builtins()
        assert registry._builtins_registered is True
        assert set(BUILTIN_CHANNELS) <= set(registry._channels)

    def test_a_failed_registration_does_not_latch_empty(self, monkeypatch):
        """A transient import failure must be retried, not remembered."""
        from robothor.engine.channels import registry

        reset_channels()
        calls = {"n": 0}
        real = registry.register_channel

        def _fail_once(name, channel, *, builtin=False):  # noqa: ANN001, ANN202
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient import failure")
            real(name, channel, builtin=builtin)

        monkeypatch.setattr(registry, "register_channel", _fail_once)
        assert get_channel("telegram") is None
        assert registry._builtins_registered is False, "a transient failure latched"

        monkeypatch.setattr(registry, "register_channel", real)
        assert get_channel("telegram") is not None


class TestBuiltinsAreProtected:
    def test_a_plugin_may_not_shadow_a_builtin_channel(self, install, monkeypatch):
        from robothor.engine.channels import BUILTIN_CHANNELS
        from robothor.engine.channels.telegram import TelegramChannel
        from robothor.plugins import load_plugins

        install({"telegram": _PluginChannel("telegram")})
        loaded = load_plugins(reserved_names=set(BUILTIN_CHANNELS))
        assert loaded.failures, "a plugin claiming 'telegram' was accepted"
        assert any("reserved" in f.reason for f in loaded.failures)

        _enable(monkeypatch, "telegram")
        assert isinstance(get_channel("telegram"), TelegramChannel)


class TestTheEnabledSetIsDeclaredConfiguration:
    def test_robothor_channels_is_a_declared_setting(self):
        from robothor.settings.registry import field_index

        assert "ROBOTHOR_CHANNELS" in field_index()

    def test_it_resolves_through_get_settings(self, monkeypatch):
        from robothor.settings import get_settings, reset_settings

        monkeypatch.setenv("ROBOTHOR_CHANNELS", "acme")
        reset_settings()
        assert get_settings().channels.enabled == "acme"


class TestTheGenerationGuardIsActuallyExercised:
    """The first version of the reload test called ``reset_channels()`` in its
    fixture, which clears the built-channel cache the generation key exists to
    invalidate — so removing ``and built[0] == current`` from
    ``registry.get_channel`` left the whole channel suite green. It was the one
    mutation of ten that survived. These tests never reset between reloads."""

    @pytest.fixture
    def install_no_reset(self, monkeypatch):
        def _install(channels: dict):
            from robothor.plugins import loader

            monkeypatch.setattr(loader, "_discover", lambda: [_ChannelEP(channels)])
            reload_plugins()

        yield _install
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        reset_channels()

    def test_a_reload_replaces_the_served_channel_without_a_registry_reset(
        self, install_no_reset, monkeypatch
    ):
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_CHANNELS", "acme")
        reset_settings()
        reset_channels()

        first = _PluginChannel()
        install_no_reset({"acme": first})
        assert get_channel("acme") is first

        second = _PluginChannel()
        install_no_reset({"acme": second})
        # No reset_channels() here: the generation key is the ONLY thing that
        # can invalidate the cached object.
        assert get_channel("acme") is second, (
            "the cached channel outlived reload_plugins() — the generation key is inert"
        )

    def test_a_repeat_lookup_within_one_generation_is_the_same_object(
        self, install_no_reset, monkeypatch
    ):
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_CHANNELS", "acme")
        reset_settings()
        reset_channels()

        install_no_reset({"acme": _PluginChannel()})
        assert get_channel("acme") is get_channel("acme")


class TestDiscoveryIsWarmedOffTheDeliveryPath:
    """``_plugin_channels()`` runs ``entry_points()`` and ``ep.load()`` — which
    imports third-party modules — and it sat inside an ``await`` in
    ``deliver()``. Blocking the loop on a plugin import while a briefing is
    going out is the stall the engine's async rule exists to prevent."""

    @pytest.mark.asyncio
    async def test_warm_channels_resolves_discovery_off_the_lookup_path(self, install, monkeypatch):
        from robothor.engine.channels import warm_channels
        from robothor.settings import reset_settings

        install({"acme": _PluginChannel()})
        monkeypatch.setenv("ROBOTHOR_CHANNELS", "acme")
        reset_settings()
        reset_channels()

        await warm_channels()

        # Discovery is cached now: a lookup must not re-enter the loader.
        from robothor.plugins import loader

        def _boom():
            raise AssertionError("discovery ran on the delivery path")

        monkeypatch.setattr(loader, "_discover", _boom)
        assert get_channel("acme") is not None

    @pytest.mark.asyncio
    async def test_warming_never_raises(self, monkeypatch):
        from robothor.engine.channels import warm_channels
        from robothor.plugins import loader

        def _boom():
            raise RuntimeError("a broken distribution")

        monkeypatch.setattr(loader, "_discover", _boom)
        reset_channels()
        await warm_channels()  # a bad plugin must not stop boot

    def test_the_daemon_warms_the_registry(self):
        """AST, not a substring: ``"warm_channels" in getsource(daemon)`` passed
        on the docstring that explains why it is called."""
        from robothor.engine.tests.astcheck import called_names, function_def

        branch = function_def("robothor.engine.daemon", "_start_channels")
        assert "warm_channels" in called_names(branch), (
            "the daemon no longer warms the channel registry, so plugin discovery "
            "runs inside the first delivery that names a plugin channel"
        )

    @pytest.mark.asyncio
    async def test_warming_cannot_outlast_its_budget(self, monkeypatch):
        """``warm_channels`` sits between "all subsystems started" and
        ``READY=1``. A plugin that blocks at import time held the daemon there
        forever, and systemd killed the unit for a timeout naming the engine."""
        import asyncio
        import time

        from robothor.engine.channels import registry, warm_channels

        monkeypatch.setattr(registry, "WARM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(registry, "_plugin_channels", lambda: time.sleep(5))
        reset_channels()

        started = asyncio.get_running_loop().time()
        await warm_channels()
        assert asyncio.get_running_loop().time() - started < 2.0, (
            "warm_channels waited out a hanging plugin import"
        )
