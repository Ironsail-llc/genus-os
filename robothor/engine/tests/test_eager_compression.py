"""Eager tool compression — thinning that never destroys the only copy.

The 2026-08-24 token audit measured re-sent tool results at 28% of ALL weekly
input (avg 7.4 sends per result) and named eager compression the second lever
(~8-12% incremental over offload). It shipped disabled for the same loop-bug
class the offload had, fixed in #359.

One design gap made the audit's "lossless in combination" claim false as
specified: offload only covers results ABOVE its threshold (8K). A mid-size
result (600-8,000 chars) thinned to a one-line summary lost its content with
NO recovery path — nothing on disk, nothing to read_file back. Thinning now
SPILLS: when a session has offloading configured, the full content is written
to disk first and the summary carries the standard retrieval stub. The
read-back is loop-exempt via the session ledger, same as any offload
artifact. Only with offloading disabled does thinning stay lossy — the
pre-existing contract, unchanged.
"""

from __future__ import annotations

from pathlib import Path

from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession


def _session(offload_threshold: int = 8000) -> AgentSession:
    return AgentSession(
        agent_id="probe",
        trigger_type=TriggerType.MANUAL,
        tool_offload_threshold=offload_threshold,
    )


def _add_tool_msg(s: AgentSession, content: str, tool: str = "list_tasks") -> None:
    s.record_tool_call(
        tool_name=tool, tool_input={}, tool_output={"content": content}, tool_call_id="t"
    )


def test_thinning_spills_to_disk_and_keeps_the_retrieval_stub():
    s = _session()
    _add_tool_msg(s, "x" * 3000)  # mid-size: below offload 8K, above summary min
    _add_tool_msg(s, "current-iteration result")

    saved = s.thin_previous_tool_results(protect_after_index=len(s.messages) - 1)

    assert saved > 0
    thinned = s.messages[-2]["content"]
    assert "[Full output:" in thinned, "thinned content lost its only copy"
    path = thinned.split("[Full output: ")[1].split(" —")[0]
    assert Path(path).read_text().count("x") >= 2900

    # and the spill is loop-exempt like any offload artifact
    assert s._is_offload_readback("read_file", {"path": path})


def test_thinning_without_offload_stays_lossy_as_before():
    """tool_offload_threshold=0 = no disk machinery; the pre-existing lossy
    contract is unchanged rather than silently writing temp files."""
    s = _session(offload_threshold=0)
    _add_tool_msg(s, "y" * 3000)
    _add_tool_msg(s, "current")

    saved = s.thin_previous_tool_results(protect_after_index=len(s.messages) - 1)
    assert saved > 0
    assert "[Full output:" not in s.messages[-2]["content"]


def test_current_iteration_is_protected():
    s = _session()
    _add_tool_msg(s, "z" * 3000)
    idx = len(s.messages) - 1
    s.thin_previous_tool_results(protect_after_index=idx - 1)
    assert "z" * 100 in s.messages[idx]["content"], "the protected message was thinned"


def test_small_results_are_untouched():
    s = _session()
    _add_tool_msg(s, "short result")
    _add_tool_msg(s, "current")
    saved = s.thin_previous_tool_results(protect_after_index=len(s.messages) - 1)
    assert saved == 0
    assert (
        s.messages[-2]["content"].endswith("short result")
        or "short result" in s.messages[-2]["content"]
    )


def test_an_offload_stub_is_never_rethinned():
    """An already-offloaded message IS the summary+path; thinning it again
    would destroy the pointer."""
    s = _session(offload_threshold=100)
    _add_tool_msg(s, "w" * 500)  # offloads immediately
    stub_before = s.messages[-1]["content"]
    _add_tool_msg(s, "current")
    s.thin_previous_tool_results(protect_after_index=len(s.messages) - 1)
    assert s.messages[-2]["content"] == stub_before


class TestFleetDefaultPlumbing:
    @staticmethod
    def _flag(v2: dict) -> bool:
        from robothor.engine.config import manifest_to_agent_config

        return manifest_to_agent_config({"id": "p", "v2": v2}).eager_tool_compression

    def test_env_supplies_the_default(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_EAGER_TOOL_COMPRESSION", "1")
        assert self._flag({}) is True

    def test_manifest_false_beats_env(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_EAGER_TOOL_COMPRESSION", "1")
        assert self._flag({"eager_tool_compression": False}) is False

    def test_default_stays_off(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_EAGER_TOOL_COMPRESSION", raising=False)
        assert self._flag({}) is False
