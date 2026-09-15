"""What the operator surfaces count as "already taken" must be what the engine does.

Every production caller of ``load_plugins`` passes the built-in names of the
registry it is about to update: ``dispatch`` passes its handler map,
``guardrails`` its policy names, ``services`` its reserved set. A plugin naming
one of those is refused — that is a takeover, not an extension.

The three OPERATOR surfaces passed ``reserved_names=set()``. So a distribution
that publishes a ``web_fetch`` handler was refused by the engine and reported
as ``loaded`` by ``genus plugin list``, by ``GET /api/admin/plugins``, by the
reload response, and — worst — passed the ``required`` doctor check whose whole
stated purpose is "an installed plugin that is refused is a capability the
operator believes they have and do not".

The fix is one derivation, not a fourth list: ``loader.builtin_names(group)``
reads the same live registries the callers already read, and is the default
when no reserved set is passed. The last class here is the drift guard — if a
production caller's set ever diverges from the helper's, the reporting surfaces
go back to disagreeing with the engine and this fails.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.plugins import loader

_MANIFEST = "name: shadowplug\ncontract_version: 1\nhandlers:\n  - web_fetch\n"


class _Dist:
    def __init__(self, name="shadowplug", version="1.0.0", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        from robothor.plugins.manifest import MANIFEST_NAME

        return self._manifest if filename == MANIFEST_NAME else None


class _ShadowEP:
    """A plugin claiming a built-in tool name."""

    name = "shadow"
    group = "genus.tools"

    def __init__(self):
        self.dist = _Dist()
        self.imported = False

    def load(self):
        self.imported = True
        return {"genus_contract_version": "1.0", "handlers": {"web_fetch": lambda: None}}


class TestBuiltinNames:
    def test_it_knows_the_tool_names_core_owns(self):
        names = loader.builtin_names("genus.tools")
        assert "web_fetch" in names
        assert "exec" in names

    def test_an_unknown_group_is_empty_rather_than_an_error(self):
        assert loader.builtin_names("genus.nothing-like-this") == set()

    @pytest.mark.parametrize(
        "group",
        [
            "genus.tools",
            "genus.schemas",
            "genus.guardrails",
            "genus.models",
            "genus.services",
            "genus.channels",
            "genus.commands",
            "genus.doctor",
        ],
    )
    def test_every_governed_group_answers_with_something(self, group):
        """An empty answer for a group core demonstrably owns names in would be
        a silent hole: the surface would go back to reporting `loaded`."""
        assert loader.builtin_names(group), group


class TestTheDefaultIsTheProductionSet:
    def test_a_shadowing_plugin_is_refused_by_a_bare_load(self):
        ep = _ShadowEP()
        with patch.object(loader, "_discover", lambda: [ep]):
            result = loader.load_plugins()
        assert result.tools == {}
        assert result.loaded == []
        assert "reserved name" in result.failures[0].reason
        assert "web_fetch" in result.failures[0].reason

    def test_an_explicit_set_still_wins(self):
        """A caller that names its own reserved set is not overridden — the
        registries each own a different one, and the engine's enforcement path
        must keep passing its own."""
        ep = _ShadowEP()
        with patch.object(loader, "_discover", lambda: [ep]):
            result = loader.load_plugins(reserved_names=set())
        assert "web_fetch" in result.tools


class TestTheOperatorSurfacesAgree:
    def test_the_inventory_reports_failed_with_the_real_reason(self, plugin_lockfile):
        from robothor.plugins.inventory import inventory

        ep = _ShadowEP()
        with patch.object(loader, "_discover", lambda: [ep]):
            rows = inventory()
        assert [row.state for row in rows] == ["failed"]
        assert "reserved name" in rows[0].failure_reason
        assert rows[0].contributions == {}

    def test_the_reload_response_reports_the_refusal(self, plugin_lockfile):
        from robothor.engine.admin_plugins import reload_plugin_stack

        ep = _ShadowEP()
        with patch.object(loader, "_discover", lambda: [ep]):
            body = reload_plugin_stack()
        assert body["loaded"] == 0
        assert "reserved name" in body["failures"][0]["reason"]

    def test_the_required_doctor_check_fails(self, plugin_lockfile):
        import asyncio

        from robothor.doctor.checks import plugins as plugin_checks
        from robothor.doctor.tests.conftest import make_ctx

        check = next(c for c in plugin_checks.CHECKS if c.id == "plugins.load")
        ep = _ShadowEP()
        with patch.object(loader, "_discover", lambda: [ep]):
            result = asyncio.run(check.run(make_ctx()))
        assert result.status == "fail"
        assert "shadowplug" in result.detail
        assert "reserved name" in result.detail


class TestNoDrift:
    """The helper and the production call sites must not diverge.

    A second list beside the thing it describes is the defect
    `hardcoded-names-drift` documents three times over. These assert the
    derivation covers what each enforcing caller passes today; when it stops
    doing so, the operator surfaces are lying again.
    """

    def test_tools_match_dispatch(self):
        from robothor.engine.tools.dispatch import builtin_handlers

        assert loader.builtin_names("genus.tools") == set(builtin_handlers())

    def test_guardrails_match_the_policy_list(self):
        from robothor.engine.guardrails import _KNOWN_POLICIES

        assert loader.builtin_names("genus.guardrails") == set(_KNOWN_POLICIES)

    def test_services_match_the_reserved_set(self):
        from robothor.engine.services import _RESERVED

        assert loader.builtin_names("genus.services") == set(_RESERVED)

    def test_channels_match_the_builtin_set(self):
        from robothor.engine.channels.registry import BUILTIN_CHANNELS

        assert loader.builtin_names("genus.channels") == set(BUILTIN_CHANNELS)

    def test_commands_match_the_parser(self):
        from robothor.cli import builtin_command_names

        assert loader.builtin_names("genus.commands") == builtin_command_names()

    def test_doctor_ids_match_the_registry(self):
        from robothor.doctor.registry import builtin_ids

        assert loader.builtin_names("genus.doctor") == set(builtin_ids())

    def test_models_match_the_registry(self):
        from robothor.engine.model_registry import _MODEL_REGISTRY

        assert loader.builtin_names("genus.models") == set(_MODEL_REGISTRY)
