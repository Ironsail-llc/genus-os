"""A call that has already been made, answered the same way, is not a step.

Profiled from the bench pod's ``agent_run_steps`` for `sam3_debug` (2026-09-15,
killed at its 1200s ceiling with nothing written): the SAME `exec` —
``cd <workspace> && python3 test_sam3.py 2>&1``, requested timeout 900 — ran
NINE times; the same ``read_file`` of one module ran six times, and four other
files two to four times each. 86 tool calls consumed 435s of a 1200s budget and
the last thing the run did before it was killed was another ``read_file``.

Nothing in the engine could see that. ``Scratchpad``'s no-progress detector
notices a repeated (tool, args, result) triple and writes a line into its
summary — useful, but it is narration, not a decision, and by the time it fires
the repeats have already been paid for. ``dedup.py`` is cross-RUN agent dedup
and unrelated.

Two rules, both narrow on purpose:

* a read whose target has not changed since the last identical read is answered
  from what the run already has — but NEVER by withholding content the model can
  no longer see. If compaction took the earlier result out of the conversation,
  the full content comes back. A guard that blinds the model is worse than the
  repeat it prevents.
* an ``exec`` whose previous output was byte-identical is NOTED on the third
  occurrence and still runs, because a command may have side effects the engine
  cannot see. Only after four identical outputs is a fifth refused, and the
  refusal names what to do: change something.

Write tools are never touched.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _Session:
    """The two things the guard reads off a live ``AgentSession``."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._step_counter = 0

    def record(self, result: dict[str, Any]) -> None:
        """What ``AgentSession.record_tool_call`` puts in the conversation."""
        self._step_counter += 1
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call-{self._step_counter}",
                "content": json.dumps(result, default=str),
            }
        )


def _guard(mode: str = "enforce", session: Any = None) -> Any:
    from robothor.engine.repeat_guard import RepeatGuard

    return RepeatGuard(mode=mode, session=session or _Session())


def _run(guard: Any, tool: str, args: dict[str, Any], result: dict[str, Any], ws: Any) -> Any:
    """One full tool call through the guard: decide, run, record."""
    decision = guard.before(tool, args, workspace=ws)
    if decision is not None and decision.result is not None:
        return decision
    guard.after(tool, args, result, workspace=ws)
    guard.session.record(result)
    return decision


# ── Reads ──────────────────────────────────────────────────────────────────


class TestAnUnchangedRead:
    def test_the_first_read_is_never_guarded(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("print(1)\n", encoding="utf-8")
        guard = _guard()
        assert guard.before("read_file", {"path": str(target)}, workspace=tmp_path) is None

    def test_a_repeat_is_answered_short_while_the_first_result_is_in_context(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "mod.py"
        target.write_text("print(1)\n" * 20, encoding="utf-8")
        args = {"path": str(target)}
        first = {"content": target.read_text(encoding="utf-8"), "path": str(target), "chars": 180}

        guard = _guard()
        _run(guard, "read_file", args, first, tmp_path)

        decision = guard.before("read_file", args, workspace=tmp_path)
        assert decision is not None
        assert decision.result is not None
        assert decision.result["unchanged_since_step"] == 1
        assert "unchanged" in decision.result["note"]
        # The content is still in the conversation, so it is not resent.
        assert "content" not in decision.result

    def test_the_full_content_comes_back_when_compaction_removed_it(self, tmp_path: Path) -> None:
        """The whole point. A short 'you already read this' pointing at nothing
        is how a guard turns a repeat into a blind agent."""
        target = tmp_path / "mod.py"
        body = "print(1)\n" * 20
        target.write_text(body, encoding="utf-8")
        args = {"path": str(target)}
        first = {"content": body, "path": str(target), "chars": len(body)}

        guard = _guard()
        _run(guard, "read_file", args, first, tmp_path)
        guard.session.messages.clear()  # compaction / thinning took it

        decision = guard.before("read_file", args, workspace=tmp_path)
        assert decision is not None
        assert decision.result is not None
        assert decision.result["content"] == body
        assert decision.result["unchanged_since_step"] == 1

    def test_a_changed_file_is_read_again_normally(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("print(1)\n", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        _run(guard, "read_file", args, {"content": "print(1)\n"}, tmp_path)

        target.write_text("print(2)\nprint(3)\n", encoding="utf-8")
        assert guard.before("read_file", args, workspace=tmp_path) is None

    def test_a_vanished_file_is_read_again_so_the_error_is_the_tools(self, tmp_path: Path) -> None:
        target = tmp_path / "mod.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        target.unlink()
        assert guard.before("read_file", args, workspace=tmp_path) is None

    def test_different_arguments_are_a_different_call(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a", encoding="utf-8")
        (tmp_path / "b.py").write_text("b", encoding="utf-8")
        guard = _guard()
        _run(guard, "read_file", {"path": str(tmp_path / "a.py")}, {"content": "a"}, tmp_path)
        assert (
            guard.before("read_file", {"path": str(tmp_path / "b.py")}, workspace=tmp_path) is None
        )

    def test_a_directory_listing_is_guarded_the_same_way(self, tmp_path: Path) -> None:
        args = {"path": str(tmp_path)}
        guard = _guard()
        _run(guard, "list_directory", args, {"entries": [], "count": 0}, tmp_path)
        decision = guard.before("list_directory", args, workspace=tmp_path)
        assert decision is not None

    def test_an_errored_read_is_never_remembered(self, tmp_path: Path) -> None:
        """Nothing was learned, so a retry is not a repeat."""
        args = {"path": str(tmp_path / "gone.py")}
        guard = _guard()
        guard.after("read_file", args, {"error": "Failed to read file: nope"}, workspace=tmp_path)
        assert guard.before("read_file", args, workspace=tmp_path) is None

    def test_a_huge_result_is_not_tracked_so_it_can_never_be_withheld(self, tmp_path: Path) -> None:
        """Holding megabytes per key to maybe resend them is the wrong trade;
        an untracked read simply runs again."""
        from robothor.engine.repeat_guard import MAX_TRACKED_CHARS

        target = tmp_path / "big.txt"
        body = "x" * (MAX_TRACKED_CHARS + 10)
        target.write_text(body, encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        _run(guard, "read_file", args, {"content": body}, tmp_path)
        assert guard.before("read_file", args, workspace=tmp_path) is None


# ── exec ───────────────────────────────────────────────────────────────────


class TestIdenticalExec:
    ARGS = {"command": "python3 test_sam3.py 2>&1", "timeout": 900}
    OUT = {"stdout": "AssertionError: shapes differ\n", "stderr": "", "exit_code": 1}

    def test_the_third_identical_run_is_noted_and_still_runs(self, tmp_path: Path) -> None:
        guard = _guard()
        for _ in range(2):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        decision = guard.before("exec", self.ARGS, workspace=tmp_path)
        assert decision is not None
        assert decision.result is None, "exec may have side effects; it must still run"
        # Counted honestly: at this point it HAS run twice and is about to run
        # a third time. A note that rounds that up to three would be the first
        # false number the agent reads from the engine.
        assert "run 2 times with identical output" in decision.note
        assert "3rd time" in decision.note

    def test_the_note_is_queued_for_the_conversation(self, tmp_path: Path) -> None:
        guard = _guard()
        for _ in range(2):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        guard.before("exec", self.ARGS, workspace=tmp_path)
        assert guard.pending_notes
        assert "identical output" in guard.pending_notes[0]

    def test_the_fifth_identical_run_is_refused(self, tmp_path: Path) -> None:
        guard = _guard()
        for _ in range(4):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        decision = guard.before("exec", self.ARGS, workspace=tmp_path)
        assert decision is not None
        assert decision.result is not None
        assert "error" in decision.result
        assert "change" in decision.result["error"]

    def test_a_refusal_names_how_many_times_it_already_ran(self, tmp_path: Path) -> None:
        guard = _guard()
        for _ in range(4):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        decision = guard.before("exec", self.ARGS, workspace=tmp_path)
        assert decision is not None and decision.result is not None
        assert decision.result["identical_runs"] == 4

    def test_a_different_output_resets_the_counter(self, tmp_path: Path) -> None:
        guard = _guard()
        for _ in range(3):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        _run(guard, "exec", self.ARGS, {**self.OUT, "exit_code": 0}, tmp_path)
        # The command started behaving differently: the run is making progress,
        # and the count starts again from there.
        assert guard.before("exec", self.ARGS, workspace=tmp_path) is None

    def test_a_refusal_is_not_a_trap(self, tmp_path: Path) -> None:
        """Every argument change is a different key, so the agent always has a
        way out — which is what makes refusing a side-effecting tool defensible
        at all."""
        guard = _guard()
        for _ in range(4):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        assert guard.before("exec", self.ARGS, workspace=tmp_path) is not None
        changed = {**self.ARGS, "command": self.ARGS["command"] + " --verbose"}
        assert guard.before("exec", changed, workspace=tmp_path) is None

    def test_a_different_command_is_untouched(self, tmp_path: Path) -> None:
        guard = _guard()
        for _ in range(6):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        other = {"command": "ls -la", "timeout": 30}
        assert guard.before("exec", other, workspace=tmp_path) is None

    def test_a_repeated_search_is_noted_but_never_refused(self, tmp_path: Path) -> None:
        """A search has no stat-able target, so it is counted by its output —
        and searching is cheap enough that refusing it would be the wrong
        trade."""
        guard = _guard()
        args = {"pattern": "def run", "glob": "*.py"}
        out = {"matches": [], "count": 0, "truncated": False}
        for _ in range(2):
            _run(guard, "search_files", args, out, tmp_path)
        decision = guard.before("search_files", args, workspace=tmp_path)
        assert decision is not None
        assert decision.result is None

        for _ in range(8):
            _run(guard, "search_files", args, out, tmp_path)
        later = guard.before("search_files", args, workspace=tmp_path)
        assert later is None or later.result is None, "a search is never refused"


# ── The allow-list ─────────────────────────────────────────────────────────


class TestWhatItNeverTouches:
    def test_write_file_is_never_guarded(self, tmp_path: Path) -> None:
        guard = _guard()
        args = {"path": str(tmp_path / "out.md"), "content": "same"}
        for _ in range(9):
            _run(guard, "write_file", args, {"success": True}, tmp_path)
        assert guard.before("write_file", args, workspace=tmp_path) is None

    def test_the_guarded_set_holds_no_side_effecting_tool(self) -> None:
        from robothor.engine.repeat_guard import GUARDED_TOOLS

        for name in ("write_file", "web_fetch", "web_search", "view_image"):
            assert name not in GUARDED_TOOLS

    def test_only_exec_can_ever_be_refused(self) -> None:
        from robothor.engine.repeat_guard import GUARDED_TOOLS, REFUSABLE_TOOLS

        assert frozenset({"exec"}) == REFUSABLE_TOOLS
        assert REFUSABLE_TOOLS <= GUARDED_TOOLS

    def test_an_unknown_tool_is_not_guarded(self, tmp_path: Path) -> None:
        guard = _guard()
        assert guard.before("send_notification", {"text": "hi"}, workspace=tmp_path) is None


# ── The ladder ─────────────────────────────────────────────────────────────


class TestTheLadder:
    def test_off_decides_nothing(self, tmp_path: Path) -> None:
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard(mode="off")
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        assert guard.before("read_file", args, workspace=tmp_path) is None

    def test_observe_runs_everything_and_returns_no_decision(self, tmp_path: Path) -> None:
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard(mode="observe")
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        assert guard.before("read_file", args, workspace=tmp_path) is None

    def test_observe_logs_what_enforce_would_have_done_at_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """At INFO this would be invisible in the benchmark container, which is
        the only place anyone reads it."""
        caplog.set_level(logging.WARNING, logger="robothor.engine.repeat_guard")
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard(mode="observe")
        guard.run_id = "run-42"
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        guard.before("read_file", args, workspace=tmp_path)

        shadow = [r for r in caplog.records if "step-efficiency observe" in r.getMessage()]
        assert shadow
        assert shadow[0].levelno == logging.WARNING
        assert "run-42" in shadow[0].getMessage()
        assert "read_file" in shadow[0].getMessage()

    def test_observe_never_refuses_an_exec(self, tmp_path: Path) -> None:
        guard = _guard(mode="observe")
        args = {"command": "true"}
        out = {"stdout": "", "exit_code": 0}
        for _ in range(9):
            _run(guard, "exec", args, out, tmp_path)
        assert guard.before("exec", args, workspace=tmp_path) is None


# ── What the sweep counts ──────────────────────────────────────────────────


class TestItIsCounted:
    def test_every_decision_bumps_a_counter(self, tmp_path: Path) -> None:
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        guard.before("read_file", args, workspace=tmp_path)
        assert guard.counters["read_file:answered"] == 1

    def test_observe_counts_too_so_a_sweep_can_compare(self, tmp_path: Path) -> None:
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard(mode="observe")
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        guard.before("read_file", args, workspace=tmp_path)
        assert guard.counters["read_file:answered"] == 1

    def test_the_decision_lands_in_the_guardrail_event_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reused, not invented: agent_guardrail_events is what the flag's
        evidence source reads."""
        import robothor.engine.tracking as tracking

        rows: list[dict[str, Any]] = []
        monkeypatch.setattr(
            tracking,
            "log_guardrail_event",
            lambda run_id, guardrail_name, action, **kw: rows.append(
                {"run_id": run_id, "guardrail_name": guardrail_name, "action": action, **kw}
            ),
        )
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        guard.run_id = "run-9"
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        guard.before("read_file", args, workspace=tmp_path)

        assert rows
        assert rows[0]["guardrail_name"] == "repeat_guard"
        assert rows[0]["tool_name"] == "read_file"
        assert rows[0]["mode"] == "enforce"

    def test_observe_writes_an_observed_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import robothor.engine.tracking as tracking

        rows: list[dict[str, Any]] = []
        monkeypatch.setattr(
            tracking,
            "log_guardrail_event",
            lambda run_id, guardrail_name, action, **kw: rows.append({"action": action}),
        )
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard(mode="observe")
        guard.run_id = "run-9"
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        guard.before("read_file", args, workspace=tmp_path)
        assert rows and rows[0]["action"] == "observed"

    def test_a_dead_database_never_breaks_a_tool_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import robothor.engine.tracking as tracking

        def _boom(*a: Any, **kw: Any) -> None:
            raise RuntimeError("no database here")

        monkeypatch.setattr(tracking, "log_guardrail_event", _boom)
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        assert guard.before("read_file", args, workspace=tmp_path) is not None


# ── Wiring ─────────────────────────────────────────────────────────────────


class TestItIsWiredIntoDispatch:
    def test_dispatch_consults_the_guard_before_the_handler_runs(self) -> None:
        import pathlib

        import robothor.engine.tools.dispatch as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        before = body.index("guard_for_run(")
        handler_call = body.index("await handler(args, ctx)")
        assert before < handler_call, "the guard must decide before the tool runs"

    def test_the_runner_drains_the_queued_notes(self) -> None:
        """A note the model never sees is a log line."""
        import pathlib

        import robothor.engine.runner as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        assert "drain_repeat_notes(session)" in body

    def test_a_run_with_no_live_session_is_simply_unguarded(self) -> None:
        from robothor.engine.repeat_guard import guard_for_run

        assert guard_for_run("no-such-run-id") is None

    def test_the_guard_is_per_run_state_on_the_session(self) -> None:
        from robothor.engine import session_registry
        from robothor.engine.repeat_guard import guard_for_run

        session = _Session()
        session.run_id = "run-abc"  # type: ignore[attr-defined]
        session_registry.register(session)  # type: ignore[arg-type]
        try:
            first = guard_for_run("run-abc")
            second = guard_for_run("run-abc")
            assert first is not None
            assert first is second
        finally:
            session_registry.unregister("run-abc")
