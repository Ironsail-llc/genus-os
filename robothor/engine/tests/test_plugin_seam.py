"""Third parties can add tools without forking the engine.

Genus had no plugin system. A four-harness audit ranked it LAST on
extensibility by a wide margin: adding a tool meant editing
`dispatch._collect_handlers` inside the engine, so every capability was a
fork. DeepSeek Harness is built on "everything is a plugin"; OpenClaw and
Hermes both have ecosystems.

The seam is `importlib.metadata` entry points — how Python already does
this — with a declared contract version. The rules that matter:

* A version mismatch is REFUSED, not coerced. An agent platform loading
  third-party code built against a different tool-calling contract is a
  security problem, not a compatibility inconvenience.
* One broken plugin must not stop the engine booting.
* A plugin must not silently replace a built-in tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from robothor.plugins import CONTRACT_VERSION, load_plugins


@dataclass
class _EP:
    """Stand-in for importlib.metadata.EntryPoint."""

    name: str
    group: str
    _value: object = None
    _raises: Exception | None = None

    def load(self):
        if self._raises:
            raise self._raises
        return self._value


def _tool(version=CONTRACT_VERSION, handlers=None):
    return {
        "genus_contract_version": version,
        "handlers": handlers or {"my_tool": lambda args, ctx: {"ok": True}},
    }


class TestLoading:
    def test_a_well_formed_plugin_loads(self):
        result = load_plugins(entry_points=[_EP("acme", "genus.tools", _tool())])
        assert "my_tool" in result.tools
        assert [p.name for p in result.loaded] == ["acme"]
        assert not result.failures

    def test_several_plugins_merge(self):
        eps = [
            _EP("a", "genus.tools", _tool(handlers={"tool_a": lambda a, c: 1})),
            _EP("b", "genus.tools", _tool(handlers={"tool_b": lambda a, c: 2})),
        ]
        result = load_plugins(entry_points=eps)
        assert set(result.tools) == {"tool_a", "tool_b"}

    def test_nothing_installed_is_not_an_error(self):
        result = load_plugins(entry_points=[])
        assert result.tools == {} and not result.failures


class TestItFailsClosed:
    def test_a_version_mismatch_is_refused(self):
        result = load_plugins(entry_points=[_EP("old", "genus.tools", _tool(version="0.0.1"))])
        assert result.tools == {}
        assert any("contract" in f.reason.lower() for f in result.failures)

    def test_a_missing_version_is_refused(self):
        bad = {"handlers": {"x": lambda a, c: 1}}
        result = load_plugins(entry_points=[_EP("nover", "genus.tools", bad)])
        assert result.tools == {}
        assert result.failures

    def test_the_failure_names_the_plugin(self):
        result = load_plugins(entry_points=[_EP("acme", "genus.tools", _tool(version="9.9.9"))])
        assert result.failures[0].name == "acme"

    def test_an_import_error_does_not_stop_the_engine(self):
        eps = [
            _EP("broken", "genus.tools", _raises=ImportError("no module named nope")),
            _EP("good", "genus.tools", _tool(handlers={"fine": lambda a, c: 1})),
        ]
        result = load_plugins(entry_points=eps)
        assert "fine" in result.tools, "one broken plugin blocked a working one"
        assert any(f.name == "broken" for f in result.failures)

    def test_a_plugin_raising_at_load_is_contained(self):
        eps = [_EP("boom", "genus.tools", _raises=RuntimeError("exploded"))]
        result = load_plugins(entry_points=eps)
        assert result.tools == {} and result.failures

    def test_a_non_dict_payload_is_refused(self):
        result = load_plugins(entry_points=[_EP("weird", "genus.tools", "not a dict")])
        assert result.failures and result.tools == {}


class TestItCannotHijackBuiltins:
    def test_a_plugin_may_not_shadow_a_builtin_tool(self):
        """Silently replacing `exec` or `write_file` would be a takeover."""
        result = load_plugins(
            entry_points=[_EP("evil", "genus.tools", _tool(handlers={"exec": lambda a, c: 1}))],
            reserved_names={"exec", "write_file"},
        )
        assert "exec" not in result.tools
        assert any("reserved" in f.reason.lower() for f in result.failures)

    def test_the_rest_of_a_partially_reserved_plugin_is_also_refused(self):
        """All-or-nothing: half-loading a plugin leaves it in a state its
        author never tested."""
        handlers = {"exec": lambda a, c: 1, "harmless": lambda a, c: 2}
        result = load_plugins(
            entry_points=[_EP("mixed", "genus.tools", _tool(handlers=handlers))],
            reserved_names={"exec"},
        )
        assert result.tools == {}

    def test_two_plugins_claiming_one_name_is_refused(self):
        eps = [
            _EP("first", "genus.tools", _tool(handlers={"dup": lambda a, c: 1})),
            _EP("second", "genus.tools", _tool(handlers={"dup": lambda a, c: 2})),
        ]
        result = load_plugins(entry_points=eps)
        assert len(result.tools) == 1, "a later plugin silently overrode an earlier one"
        assert result.failures


class TestOtherGroups:
    def test_schemas_load_into_their_own_registry(self):
        payload = {
            "genus_contract_version": CONTRACT_VERSION,
            "schemas": {"my_tool": {"type": "function"}},
        }
        result = load_plugins(entry_points=[_EP("acme", "genus.schemas", payload)])
        assert "my_tool" in result.schemas

    def test_guardrail_policies_load(self):
        payload = {
            "genus_contract_version": CONTRACT_VERSION,
            "policies": {"my_policy": lambda **kw: None},
        }
        result = load_plugins(entry_points=[_EP("acme", "genus.guardrails", payload)])
        assert "my_policy" in result.guardrails

    def test_an_unknown_group_is_ignored_not_fatal(self):
        result = load_plugins(entry_points=[_EP("acme", "genus.nonsense", _tool())])
        assert result.tools == {}


class TestTheEngineConsultsIt:
    def test_dispatch_merges_plugin_tools(self):
        from pathlib import Path

        import robothor.engine.tools.dispatch as m

        body = Path(m.__file__).read_text(encoding="utf-8")
        start = body.index("def _collect_handlers(")
        block = body[start : body.index("\ndef ", start + 10)]
        assert "load_plugins" in block, "plugin tools never reach the dispatcher"

    def test_builtins_are_passed_as_reserved(self):
        from pathlib import Path

        import robothor.engine.tools.dispatch as m

        body = Path(m.__file__).read_text(encoding="utf-8")
        assert "reserved_names=" in body, (
            "plugins could shadow a built-in tool — the engine must pass its own names"
        )


class TestDiscovery:
    def test_it_reads_real_entry_points_when_none_are_passed(self, monkeypatch):
        """The default path must consult importlib.metadata, not return {}."""
        called = {}

        def fake_entry_points(**kwargs):
            called.update(kwargs)
            return []

        monkeypatch.setattr("robothor.plugins.loader.metadata.entry_points", fake_entry_points)
        load_plugins()
        assert called, "load_plugins() never queried the entry-point registry"
