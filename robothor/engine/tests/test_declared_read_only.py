"""What the platform has classified as having no side effects.

One answer, three sources, and the direction of default is the whole design:
absent means WRITE. Two controls read this — the benchmark sub-agent allow-list
and the parallel-execution planner — and a name that leaked into it wrongly
would either hand a graded agent a real send or let two writes run at once.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robothor.engine.tools.constants import READONLY_TOOLS
from robothor.engine.tools.read_only import (
    adapter_read_only_tools,
    declared_read_only_tools,
    plugin_read_only_tools,
)


class TestTheUnion:
    def test_it_contains_cores_own_table(self):
        assert declared_read_only_tools() >= READONLY_TOOLS

    def test_a_write_is_not_in_it(self):
        declared = declared_read_only_tools()
        for name in ("write_file", "exec", "send_notification", "spawn_agent", "execute_code"):
            assert name not in declared, name

    def test_an_adapters_declaration_joins_it(self):
        adapter = MagicMock(read_only=["acme_lookup"])
        with patch(
            "robothor.engine.adapters.get_loaded_adapters", return_value=[adapter], create=True
        ):
            assert "acme_lookup" in declared_read_only_tools()

    def test_an_adapter_that_declares_nothing_contributes_nothing(self):
        adapter = MagicMock(read_only=[])
        with patch(
            "robothor.engine.adapters.get_loaded_adapters", return_value=[adapter], create=True
        ):
            assert declared_read_only_tools() >= READONLY_TOOLS

    def test_a_plugins_declaration_joins_it(self):
        loaded = MagicMock(read_only={"vendor_search"})
        with patch("robothor.plugins.load_plugins", return_value=loaded):
            assert "vendor_search" in declared_read_only_tools()


class TestItNeverRaises:
    """A classification that can fail closed must fail closed, not explode:
    both controls that read it run on the tool-call path."""

    def test_a_broken_adapter_loader_contributes_no_names(self):
        with patch(
            "robothor.engine.adapters.get_loaded_adapters",
            side_effect=RuntimeError("no"),
            create=True,
        ):
            assert adapter_read_only_tools() == frozenset()

    def test_a_broken_plugin_loader_contributes_no_names(self):
        with patch("robothor.plugins.load_plugins", side_effect=RuntimeError("no")):
            assert plugin_read_only_tools() == frozenset()

    def test_the_union_still_answers_when_both_are_broken(self):
        with (
            patch(
                "robothor.engine.adapters.get_loaded_adapters",
                side_effect=RuntimeError("no"),
                create=True,
            ),
            patch("robothor.plugins.load_plugins", side_effect=RuntimeError("no")),
        ):
            assert declared_read_only_tools() == READONLY_TOOLS


def test_the_benchmark_allow_list_reads_the_same_helper():
    """Not a second copy. A safety classification with two implementations is
    two answers, and the drift is what nobody sees."""
    import inspect

    from robothor.engine.tools.handlers import benchmark

    source = inspect.getsource(benchmark._adapter_declared_read_only_tools)
    assert "adapter_read_only_tools()" in source
    assert "get_loaded_adapters" not in source
