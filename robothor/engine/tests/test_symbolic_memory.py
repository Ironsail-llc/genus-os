"""Tests for symbolic short-term memory (Rip 13)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import symbolic_memory as sm
from robothor.engine.feature_flags import symbolic_memory_mode


class TestSymbolGraph:
    def test_sequential_node_ids(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        assert g.add_node("exec", "ran ls") == "n1"
        assert g.add_node("read_file", "read config") == "n2"

    def test_summary_sanitized_and_truncated(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        g.add_node("exec", 'line1\nline2 "quoted" ' + "x" * 200)
        node = g.nodes[0]
        assert "\n" not in node.summary
        assert '"' not in node.summary
        assert len(node.summary) <= 80

    def test_to_mermaid_has_nodes_and_edges(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        g.add_node("exec", "a")
        g.add_node("read_file", "b", ref_path="/tmp/x.txt")
        mmd = g.to_mermaid()
        assert mmd.startswith("flowchart TD")
        assert "n1[" in mmd and "n2[" in mmd
        assert "n1 --> n2" in mmd
        assert "⎘" in mmd  # offloaded node carries the ref marker

    def test_get_ref(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        g.add_node("exec", "a")
        g.add_node("read_file", "b", ref_path="/tmp/x.txt")
        assert g.get_ref("n2") == "/tmp/x.txt"
        assert g.get_ref("n1") is None
        assert g.get_ref("nope") is None

    def test_injection_block_mentions_recall(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        g.add_node("exec", "a")
        block = g.render_injection_block()
        assert "recall_node" in block
        assert "mermaid" in block

    def test_empty_graph_renders_nothing(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        assert g.to_mermaid() == ""
        assert g.render_injection_block() == ""

    def test_savings_accounting(self) -> None:
        g = sm.SymbolGraph(run_id="r1")
        g.add_node("exec", "tiny summary", ref_path="/tmp/x", full_chars=40000)
        s = g.savings()
        assert s["nodes"] == 1
        assert s["raw_tokens"] > s["graph_tokens"]
        assert s["saved_tokens"] > 0


class TestRegistry:
    def test_get_or_create_is_idempotent(self) -> None:
        sm.clear_graph("rX")
        g1 = sm.get_or_create_graph("rX")
        g2 = sm.get_or_create_graph("rX")
        assert g1 is g2
        sm.clear_graph("rX")
        assert sm.get_graph("rX") is None


class TestRatioConfig:
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert sm.mild_ratio() == 0.5
            assert sm.aggressive_ratio() == 0.85
            assert sm.mmd_max_token_ratio() == 0.2

    def test_env_override(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_MEMORY_MMD_MAX_TOKEN_RATIO": "0.33"}):
            assert sm.mmd_max_token_ratio() == 0.33


class TestMode:
    def test_off_when_rip13_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert symbolic_memory_mode() == "off"

    def test_observe_default(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_RIP_13_ENABLED": "1"}, clear=True):
            assert symbolic_memory_mode() == "observe"

    def test_enforce(self) -> None:
        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_13_ENABLED": "1", "ROBOTHOR_RIP_13_MODE": "enforce"},
            clear=True,
        ):
            assert symbolic_memory_mode() == "enforce"

    def test_panic_switch_forces_off(self) -> None:
        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_13_ENABLED": "1", "ROBOTHOR_DISABLE_ALL_RIPS": "1"},
            clear=True,
        ):
            assert symbolic_memory_mode() == "off"


class TestRecallNodeHandler:
    @pytest.mark.asyncio
    async def test_disabled_when_off(self) -> None:
        from robothor.engine.tools.handlers import symbolic as h

        with patch.object(h, "symbolic_memory_mode", return_value="off"):
            out = await h._recall_node({"node_id": "n1"}, MagicMock())
        assert "disabled" in out["error"]

    @pytest.mark.asyncio
    async def test_reads_node_output(self, tmp_path) -> None:
        from robothor.engine.tools.handlers import symbolic as h

        ref = tmp_path / "out.txt"
        ref.write_text("FULL OUTPUT HERE", encoding="utf-8")
        sm.clear_graph("run-9")
        graph = sm.get_or_create_graph("run-9")
        graph.add_node("exec", "did a thing", ref_path=str(ref), full_chars=9999)

        ctx = type("Ctx", (), {"run_id": "run-9"})()
        with patch.object(h, "symbolic_memory_mode", return_value="observe"):
            out = await h._recall_node({"node_id": "n1"}, ctx)
        assert out["content"] == "FULL OUTPUT HERE"
        sm.clear_graph("run-9")

    @pytest.mark.asyncio
    async def test_unknown_node(self) -> None:
        from robothor.engine.tools.handlers import symbolic as h

        sm.clear_graph("run-10")
        sm.get_or_create_graph("run-10")
        ctx = type("Ctx", (), {"run_id": "run-10"})()
        with patch.object(h, "symbolic_memory_mode", return_value="observe"):
            out = await h._recall_node({"node_id": "n99"}, ctx)
        assert "not found" in out["error"]
        sm.clear_graph("run-10")

    @pytest.mark.asyncio
    async def test_run_id_flows_through_dispatch(self, tmp_path) -> None:
        # Proves the dispatch chain threads run_id into ToolContext so the
        # handler can find the per-run graph (the fix that makes recall_node work).
        from robothor.engine.tools.dispatch import _execute_tool

        ref = tmp_path / "out.txt"
        ref.write_text("DISPATCHED FULL OUTPUT", encoding="utf-8")
        sm.clear_graph("run-disp")
        sm.get_or_create_graph("run-disp").add_node("exec", "x", ref_path=str(ref))

        with patch.dict(os.environ, {"ROBOTHOR_RIP_13_ENABLED": "1"}, clear=True):
            out = await _execute_tool(
                "recall_node",
                {"node_id": "n1"},
                run_id="run-disp",
                tenant_id="t1",
                user_role="service",
            )
        assert out["content"] == "DISPATCHED FULL OUTPUT"
        sm.clear_graph("run-disp")


class TestSessionIntegration:
    def test_record_tool_call_builds_node_in_observe(self) -> None:
        from robothor.engine.session import AgentSession

        with patch.dict(os.environ, {"ROBOTHOR_RIP_13_ENABLED": "1"}, clear=True):
            session = AgentSession(agent_id="test")
            sm.clear_graph(session.run_id)
            session.record_tool_call("exec", {"cmd": "ls"}, {"stdout": "a\nb\nc"}, "call-1")
            graph = sm.get_graph(session.run_id)
            assert graph is not None
            assert len(graph.nodes) == 1
            assert graph.nodes[0].tool_name == "exec"
            sm.clear_graph(session.run_id)

    def test_record_tool_call_no_node_when_off(self) -> None:
        from robothor.engine.session import AgentSession

        with patch.dict(os.environ, {}, clear=True):
            session = AgentSession(agent_id="test")
            sm.clear_graph(session.run_id)
            session.record_tool_call("exec", {"cmd": "ls"}, {"stdout": "x"}, "call-1")
            assert sm.get_graph(session.run_id) is None
