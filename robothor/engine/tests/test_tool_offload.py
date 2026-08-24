"""Tool-result offload — the token audit's biggest lever, and its loop bug.

Measured (7d ending 2026-08-24): 1,477 tool results (~10%) held 80% of
tool-result mass; the excess above 8K chars was 57.6M sent tokens — ~19% of
ALL weekly input — because every result is re-sent verbatim on every
subsequent LLM call (avg 7.4 sends). The offload machinery existed and
shipped disabled (`tool_offload_threshold: 0`) because of one bug:

The agent retrieves an offloaded result with read_file, as the offload stub
tells it to. The read_file result IS the huge content — which crossed the
threshold and was offloaded AGAIN to a new temp file, whose stub the agent
read again, forever. The agent could never actually see what it was told it
could retrieve.

The fix is exact, not heuristic: the session records every path it offloads
and never offloads a read_file result for one of them, with a
prefix+tempdir fallback for offload artifacts from other runs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession


def _session(threshold: int = 100) -> AgentSession:
    return AgentSession(
        agent_id="probe",
        trigger_type=TriggerType.MANUAL,
        tool_offload_threshold=threshold,
    )


def _record(session, tool_name, tool_input, content):
    return session.record_tool_call(
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output={"content": content},
        tool_call_id="tc1",
    )


def test_large_result_is_offloaded_with_a_retrieval_stub():
    s = _session(threshold=100)
    _record(s, "list_tasks", {}, "x" * 500)
    tool_msg = s.messages[-1]
    assert "[Full output:" in tool_msg["content"]
    assert "read_file" in tool_msg["content"]


def test_reading_back_an_offloaded_file_is_never_reoffloaded():
    """THE loop bug. The stub says 'use read_file to retrieve' — doing so must
    return the actual content, not another stub."""
    s = _session(threshold=100)
    _record(s, "list_tasks", {}, "y" * 500)
    stub = s.messages[-1]["content"]
    path = stub.split("[Full output: ")[1].split(" —")[0]
    real_content = Path(path).read_text()

    _record(s, "read_file", {"path": path}, real_content)
    read_msg = s.messages[-1]["content"]
    assert "[Full output:" not in read_msg, (
        "read_file of an offloaded artifact was re-offloaded — the agent can "
        "never see the content it was told it could retrieve"
    )
    assert "y" * 100 in read_msg


def test_foreign_offload_artifacts_are_also_exempt():
    """A prior run's artifact (not in this session's ledger) still matches the
    prefix+tempdir shape and must not loop either."""
    s = _session(threshold=100)
    fd_path = tempfile.mkstemp(prefix="tool_get_entity_", suffix=".txt")
    Path(fd_path[1]).write_text("z" * 500)
    _record(s, "read_file", {"path": fd_path[1]}, "z" * 500)
    assert "[Full output:" not in s.messages[-1]["content"]


def test_reading_a_normal_huge_file_still_offloads():
    """The exemption is for offload artifacts only — a genuinely huge ordinary
    file read must still be offloaded, or read_file becomes the bypass."""
    s = _session(threshold=100)
    _record(s, "read_file", {"path": "/home/user/project/big.log"}, "w" * 500)
    assert "[Full output:" in s.messages[-1]["content"]


def test_threshold_zero_disables_offload():
    s = _session(threshold=0)
    _record(s, "list_tasks", {}, "v" * 5000)
    assert "[Full output:" not in s.messages[-1]["content"]


class TestFleetDefaultPlumbing:
    """The manifest key stays authoritative; ROBOTHOR_TOOL_OFFLOAD_THRESHOLD
    supplies the fleet default so staging does not mean editing 22 manifests."""

    @staticmethod
    def _threshold(v2: dict) -> int:
        from robothor.engine.config import manifest_to_agent_config

        return manifest_to_agent_config({"id": "probe", "v2": v2}).tool_offload_threshold

    def test_env_supplies_the_default(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TOOL_OFFLOAD_THRESHOLD", "8000")
        assert self._threshold({}) == 8000

    def test_manifest_beats_env(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TOOL_OFFLOAD_THRESHOLD", "8000")
        assert self._threshold({"tool_offload_threshold": 200}) == 200

    def test_manifest_zero_beats_env(self, monkeypatch):
        """An explicit 0 in a manifest is an opt-OUT and must survive."""
        monkeypatch.setenv("ROBOTHOR_TOOL_OFFLOAD_THRESHOLD", "8000")
        assert self._threshold({"tool_offload_threshold": 0}) == 0

    def test_garbage_env_is_zero(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TOOL_OFFLOAD_THRESHOLD", "much")
        assert self._threshold({}) == 0
