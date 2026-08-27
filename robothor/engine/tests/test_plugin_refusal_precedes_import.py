"""A refused plugin must be refused BEFORE its code runs.

Every check in the loader — contract version, reserved names, name clashes —
sits AFTER `ep.load()` (loader.py:175), and `ep.load()` imports the
distribution's module into the daemon process. So each of those "fail-closed"
refusals happens only once the plugin has already executed whatever its module
body contains, inside the engine, with the engine's identity.

That inverts the shipped design's own intent, and it is the axis a plugin
platform is actually judged on: this repo declares 10 entry-point groups with
no governance in front of any of them.

A green test asserting `failures == [...]` proves nothing about ORDER. These
tests assert a side effect that can only exist if the module body ran, so they
fail if the refusal is merely reported rather than genuinely preemptive.

The manifest is deliberately the smallest thing that changes the order: a
distribution declares what it intends to contribute, and anything undeclared is
refused without importing. Not RFC #267's full model — tiers, lockfile,
signing, per-tenant enablement — which is blocked on five open operator
questions and on a second plugin author existing.
"""

from __future__ import annotations

import pytest

from robothor.plugins.loader import CONTRACT_VERSION, load_plugins


@pytest.fixture
def _enforce(monkeypatch):
    """The guarantee exists only in enforce; the ladder default is observe
    because requiring a manifest is a BREAKING change to a shipped seam."""
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")


class _FakeDist:
    def __init__(self, manifest):
        self._manifest = manifest

    def read_text(self, filename):
        if filename == "genus-plugin.yaml":
            return self._manifest
        return None


class _FakeEP:
    """An entry point whose load() has an observable side effect."""

    def __init__(self, name, group, payload, tripwire, manifest=None):
        self.name = name
        self.group = group
        self._payload = payload
        self._tripwire = tripwire
        self.dist = _FakeDist(manifest)

    def load(self):
        self._tripwire.append(self.name)  # only reachable if the body executes
        return self._payload


def _good_payload():
    return {"genus_contract_version": CONTRACT_VERSION, "handlers": {"weather_lookup": lambda: None}}


class TestAnUndeclaredContributionNeverImports:
    def test_a_plugin_with_no_manifest_is_refused_without_importing(self, _enforce):
        tripwire: list[str] = []
        ep = _FakeEP("rogue", "genus.tools", _good_payload(), tripwire, manifest=None)
        result = load_plugins(entry_points=[ep])
        assert tripwire == [], "the plugin's module body ran before it was refused"
        assert result.failures

    def test_a_tool_the_manifest_omits_is_not_registered(self, _enforce):
        """The honest limit of this gate, stated rather than glossed.

        You cannot know a module's exports without importing it, so a plugin
        that DOES ship a manifest is imported and then held to it. The
        pre-import guarantee covers the case that matters — an unmanifested
        distribution never executes at all — and the manifest is what makes
        the rest reviewable before install.
        """
        tripwire: list[str] = []
        manifest = "name: rogue\ncontract_version: 1\nhandlers:\n  - something_else\n"
        ep = _FakeEP("rogue", "genus.tools", _good_payload(), tripwire, manifest=manifest)
        result = load_plugins(entry_points=[ep])
        assert "weather_lookup" not in result.tools, "an undeclared tool was registered"
        assert result.failures


class TestADeclaredPluginStillLoads:
    """The negative control. A gate that refused everything would satisfy the
    tests above and leave the seam useless."""

    def test_a_declared_tool_loads_normally(self, _enforce):
        tripwire: list[str] = []
        manifest = "name: good\ncontract_version: 1\nhandlers:\n  - weather_lookup\n"
        ep = _FakeEP("good", "genus.tools", _good_payload(), tripwire, manifest=manifest)
        result = load_plugins(entry_points=[ep])
        assert tripwire == ["good"], "a declared plugin was not imported"
        assert "weather_lookup" in result.tools
        assert not result.failures


class TestTheLadderIsHonestAboutWhatObserveDoes:
    """observe still imports. Saying so in a test stops a dashboard reading
    "observe" from implying containment it does not have — the failure mode
    this repo has hit repeatedly."""

    def test_observe_does_not_protect(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "observe")
        tripwire: list[str] = []
        ep = _FakeEP("rogue", "genus.tools", _good_payload(), tripwire, manifest=None)
        load_plugins(entry_points=[ep])
        assert tripwire == ["rogue"], (
            "observe must still import — if it refuses, the ladder has no observe rung"
        )

    def test_off_disables_the_check_entirely(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_ENABLED", "0")
        tripwire: list[str] = []
        ep = _FakeEP("rogue", "genus.tools", _good_payload(), tripwire, manifest=None)
        result = load_plugins(entry_points=[ep])
        assert tripwire == ["rogue"]
        assert "weather_lookup" in result.tools
