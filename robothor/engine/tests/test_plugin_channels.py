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

from robothor.engine.channels import SendReceipt, get_channel, reset_channels
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
