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
    def __init__(self, name="shadowplug", manifest=_MANIFEST, version="1.0.0"):
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

    def test_schemas_match_what_the_tool_registry_reserves(self):
        """The entry with no guard, and it was 53 names short.

        ``ToolRegistry._register_all`` seeds ``_schemas`` from the MCP tool
        definitions AND ``get_engine_schemas()``; the table pointed at only the
        second, so every CRM tool (`create_person`, `approve_task`, …) was
        reserved in production and derivable by nobody.
        """
        from robothor.engine.tools.registry import ToolRegistry, builtin_schema_names

        assert loader.builtin_names("genus.schemas") == builtin_schema_names()
        with patch("robothor.plugins.load_plugins") as plugins:
            plugins.return_value.schemas = {}
            assert loader.builtin_names("genus.schemas") >= set(ToolRegistry()._schemas)

    def test_hooks_match_what_the_hook_registry_reserves(self):
        """The group whose own docstring names the attack it was not stopping.

        ``register_plugin_hooks`` reserves ``registry._python_handlers`` and the
        daemon registers three of them at boot; the table mapped the group to
        nothing, so a plugin claiming ``channel_bus.surface`` — the exact name
        that docstring calls out — was refused in production and reported as
        loaded everywhere an operator could look.
        """
        from robothor.engine.hook_registry import HookRegistry, builtin_hook_names

        registry = HookRegistry()
        registry.register_python_handler("channel_bus.surface", lambda *a, **k: None)
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=registry):
            assert builtin_hook_names() == {"channel_bus.surface"}
            assert loader.builtin_names("genus.hooks") == {"channel_bus.surface"}


def _groups_without_a_guard(source: str) -> list[str]:
    """Table entries `source` never mentions — i.e. that nothing asserts."""
    return sorted(group for group in loader._BUILTIN_SOURCES if f'"{group}"' not in source)


class TestTheDriftGuardIsComplete:
    """Every `_BUILTIN_SOURCES` entry must have an assertion in `TestNoDrift`.

    Deliberately a SEPARATE class, so that its own group literals are not part
    of the source being scanned — a meta-guard that satisfies itself is the
    vacuous-control pattern this whole review keeps finding.
    """

    def test_every_table_entry_has_a_drift_assertion(self):
        import inspect

        ungoverned = _groups_without_a_guard(inspect.getsource(TestNoDrift))
        assert not ungoverned, (
            f"these groups are in _BUILTIN_SOURCES with no assertion in TestNoDrift: "
            f"{ungoverned}. A derivation nothing compares to its caller is how the "
            "53-name genus.schemas gap survived round 1."
        )

    def test_it_would_notice_a_deleted_assertion(self):
        """Not just an added table entry.

        A hardcoded expected set only catches the add direction; deleting a
        guard during a refactor left it still matching. This proves the scan
        fails when an assertion goes away.
        """
        import inspect

        source = inspect.getsource(TestNoDrift)
        for group in loader._BUILTIN_SOURCES:
            gutted = source.replace(f'"{group}"', '"genus.deleted-assertion"')
            assert _groups_without_a_guard(gutted) == [group], group


class TestHooksAreNotCachedStale:
    """Hook handlers register during daemon boot, so a cached empty set taken
    before ``daemon.py`` registers them would freeze the round-1 gap back in.
    Every other source is fixed for the life of the process; this one is not.
    """

    def test_the_answer_follows_the_registry(self):
        from robothor.engine.hook_registry import HookRegistry

        empty = HookRegistry()
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=empty):
            assert loader.builtin_names("genus.hooks") == set()

        booted = HookRegistry()
        booted.register_python_handler("channel_bus.surface", lambda *a, **k: None)
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=booted):
            assert loader.builtin_names("genus.hooks") == {"channel_bus.surface"}

    def test_no_registry_at_all_is_an_empty_set(self):
        """A CLI process has no hook registry. The honest answer is "none"."""
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=None):
            assert loader.builtin_names("genus.hooks") == set()


class _HookEP:
    """A plugin claiming the engine's own channel plumbing."""

    name = "hook"
    group = "genus.hooks"

    def __init__(self):
        self.dist = _Dist("hookplug", manifest="name: hookplug\ncontract_version: 1\n")

    def load(self):
        return {"genus_contract_version": "1.0", "hooks": {"channel_bus.surface": lambda: None}}


class TestTheHookCaseOnEverySurface:
    """The round-1 defect verbatim, on the group the fix originally missed."""

    @pytest.fixture
    def engine_owns_the_hook(self):
        from robothor.engine.hook_registry import HookRegistry

        registry = HookRegistry()
        registry.register_python_handler("channel_bus.surface", lambda *a, **k: None)
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=registry):
            yield registry

    def test_a_bare_load_refuses_it(self, engine_owns_the_hook):
        with patch.object(loader, "_discover", lambda: [_HookEP()]):
            result = loader.load_plugins()
        assert result.hooks == {}
        assert "reserved name" in result.failures[0].reason

    def test_the_inventory_reports_failed(self, engine_owns_the_hook, plugin_lockfile):
        from robothor.plugins.inventory import inventory

        with patch.object(loader, "_discover", lambda: [_HookEP()]):
            rows = inventory()
        assert [row.state for row in rows] == ["failed"]
        assert "channel_bus.surface" in rows[0].failure_reason
        assert rows[0].contributions == {}

    def test_the_reload_response_reports_it(self, engine_owns_the_hook, plugin_lockfile):
        from robothor.engine.admin_plugins import reload_plugin_stack

        with patch.object(loader, "_discover", lambda: [_HookEP()]):
            body = reload_plugin_stack()
        assert body["loaded"] == 0
        assert "reserved name" in body["failures"][0]["reason"]

    def test_the_required_doctor_check_fails(self, engine_owns_the_hook, plugin_lockfile):
        import asyncio

        from robothor.doctor.checks import plugins as plugin_checks
        from robothor.doctor.tests.conftest import make_ctx

        check = next(c for c in plugin_checks.CHECKS if c.id == "plugins.load")
        with patch.object(loader, "_discover", lambda: [_HookEP()]):
            result = asyncio.run(check.run(make_ctx()))
        assert result.status == "fail"
        assert "hookplug" in result.detail
        assert "channel_bus.surface" in result.detail

    def test_production_still_refuses_it_too(self, engine_owns_the_hook):
        """The enforcement half, so the surfaces are proved to AGREE rather
        than merely to be strict."""
        from robothor.engine.hook_registry import register_plugin_hooks

        with patch.object(loader, "_discover", lambda: [_HookEP()]):
            registered = register_plugin_hooks(engine_owns_the_hook)
        assert registered == 0
