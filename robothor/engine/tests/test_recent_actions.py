"""Tests for engine post-run recent-actions log (cross-session visibility)."""

from __future__ import annotations

from types import SimpleNamespace

from robothor.engine.recent_actions import (
    _MAX_ENTRIES,
    record_run,
    summarize_run,
)


def _step(**kw):
    defaults = {"tool_name": None, "tool_input": None, "tool_output": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _run(agent_id="main", trigger="telegram", steps=None):
    return SimpleNamespace(
        agent_id=agent_id,
        trigger_type=trigger,
        steps=steps or [],
    )


class TestSummarize:
    def test_empty_when_no_notable_steps(self):
        run = _run(steps=[_step(tool_name="list_people", tool_input={"search": "x"})])
        assert summarize_run(run) == ""

    def test_summarizes_calendar_create(self):
        run = _run(
            steps=[
                _step(
                    tool_name="gws_calendar_create",
                    tool_input={
                        "summary": "Team Weekly",
                        "start": "2026-04-21T14:00:00-04:00",
                    },
                    tool_output={"id": "e1"},
                )
            ]
        )
        out = summarize_run(run)
        assert "[telegram]" in out
        assert "calendar_create" in out
        assert "Team Weekly" in out

    def test_marks_dedup(self):
        run = _run(
            steps=[
                _step(
                    tool_name="gws_calendar_create",
                    tool_input={"summary": "X"},
                    tool_output={"status": "deduped", "summary": "X"},
                )
            ]
        )
        assert "calendar_deduped" in summarize_run(run)

    def test_combines_multiple_clauses(self):
        run = _run(
            steps=[
                _step(
                    tool_name="gws_calendar_create",
                    tool_input={"summary": "A", "start": "2026-05-01T10:00-04:00"},
                ),
                _step(
                    tool_name="gws_gmail_send",
                    tool_input={"to": "x@y", "subject": "Confirmed"},
                    tool_output={},
                ),
                _step(tool_name="spawn_agent", tool_input={"agent_id": "email-responder"}),
            ]
        )
        out = summarize_run(run)
        assert "calendar_create" in out
        assert "email_send" in out
        assert "spawn_agent" in out


class TestRecordRun:
    def test_only_writes_for_tracked_agents(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        run = _run(
            agent_id="email-responder",
            steps=[
                _step(
                    tool_name="gws_calendar_create",
                    tool_input={"summary": "x", "start": "2026-01-01T10:00Z"},
                )
            ],
        )
        record_run(run)
        assert not (tmp_path / "brain/memory/main-recent-actions.md").exists()

    def test_writes_and_ring_buffers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))

        # Write _MAX_ENTRIES + 5 distinct runs; only last _MAX_ENTRIES kept.
        for i in range(_MAX_ENTRIES + 5):
            run = _run(
                steps=[
                    _step(
                        tool_name="create_task",
                        tool_input={"title": f"task-{i}"},
                    )
                ]
            )
            record_run(run)

        path = tmp_path / "brain/memory/main-recent-actions.md"
        assert path.exists()
        content = path.read_text()
        # Newest entry (task-24) at top; oldest kept is task-5 (0-4 trimmed).
        assert "task-24" in content
        assert "task-4" not in content
        # Non-header lines should equal _MAX_ENTRIES.
        non_header = [
            line for line in content.splitlines() if line.strip() and not line.startswith("#")
        ]
        assert len(non_header) == _MAX_ENTRIES

    def test_skips_when_no_notable_steps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        run = _run(steps=[_step(tool_name="list_people", tool_input={})])
        record_run(run)
        assert not (tmp_path / "brain/memory/main-recent-actions.md").exists()

    def test_swallows_write_errors(self, tmp_path, monkeypatch):
        # Pointing workspace at a non-writable parent forces a write failure.
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", "/dev/null/definitely-not-writable")
        run = _run(steps=[_step(tool_name="create_task", tool_input={"title": "x"})])
        # Must not raise.
        record_run(run)


class TestConcurrentRuns:
    """`_finish_run` records each run from its own worker thread, so two `main`
    runs finishing together (a heartbeat and a chat turn) write at once."""

    def test_runs_recorded_at_once_all_survive(self, tmp_path, monkeypatch):
        import threading
        import time

        from robothor.engine import recent_actions

        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        real_read = recent_actions._read_existing_lines

        def slow_read(path):
            lines = real_read(path)
            time.sleep(0.05)  # widen the read-to-write window
            return lines

        monkeypatch.setattr(recent_actions, "_read_existing_lines", slow_read)
        runs = [
            _run(steps=[_step(tool_name="create_task", tool_input={"title": f"task-{i}"})])
            for i in range(5)
        ]
        threads = [threading.Thread(target=record_run, args=(run,)) for run in runs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = (tmp_path / "brain/memory/main-recent-actions.md").read_text()
        missing = [i for i in range(5) if f"task-{i}" not in content]
        assert not missing, f"concurrent writes lost entries {missing}"

    def test_a_write_that_fails_part_way_keeps_the_previous_log(self, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        record_run(_run(steps=[_step(tool_name="create_task", tool_input={"title": "kept"})]))

        real_write = Path.write_text

        def torn_write(self, data, *args, **kwargs):
            real_write(self, data[:10], *args, **kwargs)
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", torn_write)
        record_run(_run(steps=[_step(tool_name="create_task", tool_input={"title": "new"})]))
        monkeypatch.undo()

        content = (tmp_path / "brain/memory/main-recent-actions.md").read_text()
        assert "kept" in content, "a torn write left a truncated log behind"
