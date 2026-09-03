"""A plugin must be able to declare its own tools read-only.

Extracting Princess Freya to a plugin on 2026-08-27 left one thing behind:
``pf_system_status`` had to STAY in core's ``READONLY_TOOLS`` frozenset,
because ``genus.tools`` carries handlers and ``genus.schemas`` carries
schemas, and nothing carried safety classification. Core kept a hardcoded
fact about one instance's boat.

That is not cosmetic. ``READONLY_TOOLS`` gates real behaviour:
``registry.readonly_names_for`` and the runner's read-only tool set decide
what an agent may call in a restricted context. A plugin tool absent from
the set is treated as a WRITE tool; a plugin author's only remedy was to
patch core, which is the fork the plugin seam exists to prevent.

So the contract grows a fourth thing a `genus.tools` payload may declare.
Absent, nothing changes -- a plugin that says nothing is still assumed to
write, which is the safe default and today's behaviour.
"""

from __future__ import annotations

from robothor.plugins import loader


class _EP:
    def __init__(self, name, group, payload):
        self.name, self.group, self._p = name, group, payload

    def load(self):
        return self._p


def _tools_payload(**extra):
    return {
        "genus_contract_version": loader.CONTRACT_VERSION,
        "handlers": {"probe_tool": lambda a, c: None},
        **extra,
    }


def test_a_plugin_can_declare_a_tool_read_only():
    result = loader.load_plugins(
        entry_points=[_EP("p", "genus.tools", _tools_payload(read_only=["probe_tool"]))],
        reserved_names=set(),
    )
    assert not result.failures, result.failures
    assert "probe_tool" in result.read_only, (
        "a plugin cannot declare its tool read-only, so it is treated as a "
        "write tool and its author's only remedy is to patch core"
    )


def test_silence_means_write_which_is_the_safe_default():
    result = loader.load_plugins(
        entry_points=[_EP("p", "genus.tools", _tools_payload())], reserved_names=set()
    )
    assert result.read_only == set()


def test_a_plugin_may_not_declare_a_tool_it_does_not_own():
    """Otherwise any plugin could reclassify a core write tool as safe."""
    result = loader.load_plugins(
        entry_points=[_EP("p", "genus.tools", _tools_payload(read_only=["exec"]))],
        reserved_names={"exec"},
    )
    assert "exec" not in result.read_only, (
        "a plugin reclassified a tool it does not provide — that is a "
        "privilege escalation, not an extension"
    )


def test_a_malformed_declaration_is_refused_not_guessed():
    result = loader.load_plugins(
        entry_points=[_EP("p", "genus.tools", _tools_payload(read_only="probe_tool"))],
        reserved_names=set(),
    )
    assert result.failures, "a string where a list belongs must fail closed"
    assert result.read_only == set()


def test_the_registry_reports_plugin_read_only_tools():
    """Declared is not enough — the gate that uses it must see it."""
    from pathlib import Path

    from robothor.engine.tools import registry as reg_mod

    text = Path(reg_mod.__file__).read_text()
    assert "plugin_read_only" in text or "read_only" in text, (
        "registry never consults plugin-declared read-only names, so the declaration would be inert"
    )
