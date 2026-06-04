"""Tests for Rip 10 trajectory capture."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from robothor.engine.session import AgentSession
from robothor.engine.trajectory import _to_sharegpt, save_trajectory


def _session_with_messages(messages: list[dict] | None = None) -> AgentSession:
    s = AgentSession(agent_id="test-agent", tenant_id="t1")
    s.messages = messages or [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
    ]
    return s


class TestToShareGPT:
    def test_translates_standard_roles(self) -> None:
        out = _to_sharegpt(
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"},
                {"role": "tool", "content": "t"},
            ]
        )
        assert [e["from"] for e in out] == ["system", "human", "gpt", "tool"]
        assert [e["value"] for e in out] == ["s", "u", "a", "t"]

    def test_serializes_tool_calls_when_content_missing(self) -> None:
        out = _to_sharegpt([{"role": "assistant", "tool_calls": [{"name": "x", "args": {"a": 1}}]}])
        assert "tool_calls" in out[0]["value"]
        assert '"name":' in out[0]["value"]

    def test_flattens_list_content(self) -> None:
        out = _to_sharegpt([{"role": "user", "content": [{"text": "a"}, "b"]}])
        assert "a" in out[0]["value"]
        assert "b" in out[0]["value"]

    def test_none_content_becomes_empty(self) -> None:
        out = _to_sharegpt([{"role": "assistant", "content": None}])
        assert out[0]["value"] == ""


class TestSaveTrajectory:
    def test_sample_rate_zero_returns_none(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=True):
            session = _session_with_messages()
            assert save_trajectory(session, completed=True, base=tmp_path) is None

    def test_writes_completed_file(self, tmp_path: Path) -> None:
        session = _session_with_messages()
        path = save_trajectory(session, completed=True, base=tmp_path, sample_rate_override=1.0)
        assert path is not None
        assert path.exists()
        assert path.name == "trajectory_samples.jsonl"
        assert "t1" in str(path)
        # Parse the line back to verify ShareGPT shape.
        line = path.read_text().splitlines()[0]
        record = json.loads(line)
        assert record["run_id"] == session.run_id
        assert record["agent_id"] == "test-agent"
        assert record["tenant_id"] == "t1"
        assert record["completed"] is True
        assert {"from", "value"} <= record["messages"][0].keys()

    def test_writes_failed_file(self, tmp_path: Path) -> None:
        session = _session_with_messages()
        path = save_trajectory(session, completed=False, base=tmp_path, sample_rate_override=1.0)
        assert path is not None
        assert path.name == "failed_trajectories.jsonl"

    def test_empty_session_returns_none(self, tmp_path: Path) -> None:
        session = AgentSession(agent_id="x")
        result = save_trajectory(session, completed=True, base=tmp_path, sample_rate_override=1.0)
        assert result is None

    def test_appends_subsequent_runs(self, tmp_path: Path) -> None:
        session_a = _session_with_messages()
        session_b = _session_with_messages([{"role": "user", "content": "second"}])
        save_trajectory(session_a, completed=True, base=tmp_path, sample_rate_override=1.0)
        path = save_trajectory(session_b, completed=True, base=tmp_path, sample_rate_override=1.0)
        assert path is not None
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_write_failure_returns_none(self, tmp_path: Path) -> None:
        session = _session_with_messages()
        # Make the trajectory dir a *file* so mkdir/open fails.
        bad_root = tmp_path / "blocked"
        bad_root.write_text("not a dir")
        result = save_trajectory(session, completed=True, base=bad_root, sample_rate_override=1.0)
        assert result is None

    def test_sample_rate_filter(self, tmp_path: Path) -> None:
        session = _session_with_messages()
        with patch("robothor.engine.trajectory.random.random", return_value=0.9):
            # rate=0.1, random=0.9 → 0.9 > 0.1 → skip
            result = save_trajectory(
                session, completed=True, base=tmp_path, sample_rate_override=0.1
            )
        assert result is None

        with patch("robothor.engine.trajectory.random.random", return_value=0.05):
            # rate=0.1, random=0.05 → 0.05 <= 0.1 → write
            result = save_trajectory(
                session, completed=True, base=tmp_path, sample_rate_override=0.1
            )
        assert result is not None
