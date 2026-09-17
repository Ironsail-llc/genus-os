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

    def test_a_directory_listing_is_not_short_circuited(self, tmp_path: Path) -> None:
        """A listing reports sizes the fingerprint never stats — see
        TestListDirectoryIsNeverAnsweredStale for what that cost."""
        args = {"path": str(tmp_path)}
        guard = _guard()
        _run(guard, "list_directory", args, {"entries": [], "count": 0}, tmp_path)
        decision = guard.before("list_directory", args, workspace=tmp_path)
        assert decision is None or decision.result is None

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
        # Present perfect: the note is drained into the conversation AFTER the
        # third result, so by the time the model reads it the command HAS run
        # three times. See TestTheNoteReadsCorrectlyWhereItLands.
        assert "has now run 3 times with identical output" in decision.note

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
        # Not an `error`: that key is what the runner's circuit breaker counts.
        assert decision.result["refused"] is True
        assert "change" in decision.result["reason"]

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

    def test_the_turn_drains_the_queued_notes(self) -> None:
        """A note the model never sees is a log line. The drain moved to
        `tool_turn` with the rest of the tool-call block; it still has to run
        after every result and never between them."""
        import pathlib

        import robothor.engine.tool_turn as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        assert "drain_repeat_notes(req.session)" in body

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


# ── The two enforce controls must not cancel each other ────────────────────


class TestTheClampDoesNotBlindTheGuard:
    """The review's C1: the clamp writes `timeout_note` into every clamped
    `exec` result, the remaining-seconds number shrinks on each call, and a
    digest taken over the WHOLE result therefore changes every time — so the
    counter reset on every call and nine byte-identical runs of the profiled
    command produced zero decisions. The two enforce controls cancelled each
    other in exactly the regime this task targets.
    """

    ARGS = {"command": "python3 test_sam3.py 2>&1", "timeout": 900}

    def _nine_runs(self, guard: Any, tmp_path: Path, annotate: bool) -> list[str | None]:
        actions: list[str | None] = []
        for i in range(9):
            decision = guard.before("exec", self.ARGS, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            if decision is not None and decision.result is not None:
                continue
            out: dict[str, Any] = {
                "stdout": "AssertionError: shapes differ\n",
                "stderr": "",
                "exit_code": 1,
            }
            if annotate:
                # Exactly what `_exec` attaches while the clamp is engaged.
                out["timeout_note"] = f"timeout clamped to {212 - i}s: the run has {242 - i}s left"
            guard.after("exec", self.ARGS, out, workspace=tmp_path)
            guard.session.record(out)
        return actions

    def test_nine_identical_execs_still_note_and_refuse_under_an_active_clamp(
        self, tmp_path: Path
    ) -> None:
        actions = self._nine_runs(_guard(), tmp_path, annotate=True)
        assert actions[2] == "noted", actions
        assert actions[4] == "refused", actions

    def test_the_annotated_and_bare_results_decide_identically(self, tmp_path: Path) -> None:
        """The clamp note must be invisible to the digest, not merely tolerated."""
        assert self._nine_runs(_guard(), tmp_path, annotate=True) == self._nine_runs(
            _guard(), tmp_path, annotate=False
        )

    def test_the_digest_ignores_engine_annotations_by_construction(self) -> None:
        from robothor.engine.repeat_guard import output_digest

        bare = {"stdout": "x", "stderr": "", "exit_code": 0}
        annotated = {**bare, "timeout_note": "timeout clamped to 5s: the run has 35s left"}
        assert output_digest("exec", bare) == output_digest("exec", annotated)

    def test_a_real_output_change_still_changes_the_digest(self) -> None:
        from robothor.engine.repeat_guard import output_digest

        a = {"stdout": "x", "stderr": "", "exit_code": 0}
        b = {"stdout": "y", "stderr": "", "exit_code": 0}
        assert output_digest("exec", a) != output_digest("exec", b)

    def test_the_whole_path_through_dispatch_notes_a_repeat(self, tmp_path: Path) -> None:
        """No unit above drives `_exec` itself; this one does, with a live
        watchdog so the clamp is genuinely engaged."""
        import asyncio
        from types import SimpleNamespace

        import robothor.engine.feature_flags as ff
        import robothor.engine.permissions as perms
        from robothor.engine import session_registry
        from robothor.engine.stall_watchdog import _active_watchdog_var
        from robothor.engine.tools import dispatch

        session = _Session()
        session.run_id = "run-clamp"  # type: ignore[attr-defined]
        session.step_efficiency_mode = "enforce"  # type: ignore[attr-defined]
        real_perm = perms.check_tool_permission
        real_mode = ff.step_efficiency_mode
        perms.check_tool_permission = lambda *a, **kw: None  # type: ignore[assignment]
        ff.step_efficiency_mode = lambda: "enforce"  # type: ignore[assignment]
        # The clock MOVES between calls, which is the whole point: a clamp note
        # carrying "the run has Ns left" is different text every time.
        watchdog = SimpleNamespace(elapsed_seconds=958.0, _hard_timeout=1200.0)
        token = _active_watchdog_var.set(watchdog)
        session_registry.register(session)  # type: ignore[arg-type]
        try:

            async def _calls() -> list[dict[str, Any]]:
                out = []
                for _ in range(3):
                    res = await dispatch._execute_tool(
                        "exec",
                        {"command": "echo steady", "timeout": 900},
                        run_id="run-clamp",
                        workspace=str(tmp_path),
                    )
                    session.record(res)
                    out.append(res)
                    watchdog.elapsed_seconds += 7.0
                return out

            results = asyncio.run(_calls())
        finally:
            _active_watchdog_var.reset(token)
            session_registry.unregister("run-clamp")
            perms.check_tool_permission = real_perm  # type: ignore[assignment]
            ff.step_efficiency_mode = real_mode  # type: ignore[assignment]

        assert results[0]["timeout_note"], "the clamp did not engage; the test proves nothing"
        assert results[0]["timeout_note"] != results[1]["timeout_note"], (
            "the clamp note did not change between calls; the test proves nothing"
        )
        guard = session.repeat_guard  # type: ignore[attr-defined]
        assert guard.counters["exec:noted"] == 1, dict(guard.counters)
        assert guard.pending_notes


# ── A refusal is not a tool failure ────────────────────────────────────────


class TestARefusalIsNotAFailure:
    ARGS = {"command": "python3 test_sam3.py 2>&1"}
    OUT = {"stdout": "AssertionError\n", "stderr": "", "exit_code": 1}

    def _refusal(self, tmp_path: Path) -> dict[str, Any]:
        guard = _guard()
        for _ in range(4):
            _run(guard, "exec", self.ARGS, self.OUT, tmp_path)
        decision = guard.before("exec", self.ARGS, workspace=tmp_path)
        assert decision is not None and decision.result is not None
        return decision.result

    def test_the_refusal_carries_no_error_key(self, tmp_path: Path) -> None:
        """`runner.py` does `error_msg = result.get("error")`, which feeds the
        per-tool circuit breaker. A refusal is a redirection, not a fault."""
        result = self._refusal(tmp_path)
        assert "error" not in result
        assert result["refused"] is True
        assert "change" in result["reason"]

    def test_three_refusals_do_not_tell_the_agent_to_stop_using_exec(self, tmp_path: Path) -> None:
        """The failure this prevents: a control meant to redirect ONE command
        telling a Code-task agent to abandon its only way to run code."""
        from robothor.engine.tool_outcome import record_tool_outcome

        result = self._refusal(tmp_path)
        session = _Session()
        failures: dict[str, int] = {}
        for _ in range(3):
            record_tool_outcome(
                session,  # type: ignore[arg-type]
                tool_name="exec",
                tool_args=self.ARGS,
                result=result,
                # exactly how runner.py derives it
                error_msg=result.get("error"),
                elapsed_ms=1,
                scratchpad=None,
                failures=failures,
            )
        assert failures.get("exec", 0) == 0
        assert not [m for m in session.messages if "Do NOT call it again" in str(m.get("content"))]

    def test_a_real_exec_failure_still_counts(self, tmp_path: Path) -> None:
        """The guard must not have disarmed the circuit breaker in general."""
        from robothor.engine.tool_outcome import record_tool_outcome

        session = _Session()
        failures: dict[str, int] = {}
        for _ in range(3):
            record_tool_outcome(
                session,  # type: ignore[arg-type]
                tool_name="exec",
                tool_args=self.ARGS,
                result={"error": "Command failed: boom"},
                error_msg="Command failed: boom",
                elapsed_ms=1,
                scratchpad=None,
                failures=failures,
            )
        assert failures["exec"] == 3
        assert [m for m in session.messages if "Do NOT call it again" in str(m.get("content"))]


# ── Silent commands are never refused ──────────────────────────────────────


class TestOnlyASpeakingCommandIsRefused:
    """The review's I1. Output identity is not effect identity, and the proxy
    is weakest exactly where the risk is: the side-effecting commands an agent
    runs are disproportionately SILENT (`mkdir -p`, `cp`, `rm -f`, `chmod`,
    `git add`, anything redirected to /dev/null). Four identical silent
    successes establish nothing about the fifth, whose inputs may have been
    changed by the commands in between.
    """

    SILENT = {"command": "mkdir -p results && cp a.png results/"}
    QUIET_OUT = {"stdout": "", "stderr": "", "exit_code": 0}

    def test_a_silent_command_is_never_refused(self, tmp_path: Path) -> None:
        guard = _guard()
        actions = []
        for _ in range(9):
            decision = guard.before("exec", self.SILENT, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            if decision is not None and decision.result is not None:
                continue
            guard.after("exec", self.SILENT, self.QUIET_OUT, workspace=tmp_path)
            guard.session.record(self.QUIET_OUT)
        assert "refused" not in actions, actions

    def test_a_silent_command_is_still_noted(self, tmp_path: Path) -> None:
        """The note costs nothing and may still redirect the agent."""
        guard = _guard()
        for _ in range(2):
            _run(guard, "exec", self.SILENT, self.QUIET_OUT, tmp_path)
        decision = guard.before("exec", self.SILENT, workspace=tmp_path)
        assert decision is not None and decision.action == "noted"

    def test_whitespace_only_output_counts_as_silent(self, tmp_path: Path) -> None:
        guard = _guard()
        out = {"stdout": "\n \t\n", "stderr": "", "exit_code": 0}
        actions = []
        for _ in range(7):
            decision = guard.before("exec", self.SILENT, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            if decision is not None and decision.result is not None:
                continue
            guard.after("exec", self.SILENT, out, workspace=tmp_path)
        assert "refused" not in actions, actions

    def test_the_profiled_command_still_reaches_a_refusal(self, tmp_path: Path) -> None:
        """`sam3_debug`'s nine repeats printed. 100% of the measured value is
        kept by this narrowing."""
        guard = _guard()
        args = {"command": "cd /ws && python3 test_sam3.py 2>&1", "timeout": 900}
        out = {"stdout": "AssertionError: shapes differ\n", "stderr": "", "exit_code": 1}
        actions = []
        for _ in range(6):
            decision = guard.before("exec", args, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            if decision is not None and decision.result is not None:
                continue
            guard.after("exec", args, out, workspace=tmp_path)
        assert actions[2] == "noted"
        assert actions[4] == "refused"


# ── Repeated failures are the most expensive repeat there is ───────────────


class TestIdenticalErrorsAreCountedToo:
    ARGS = {"command": "python3 slow.py", "timeout": 900}
    #: The result `_exec` actually builds on a timeout. The limit is NOT in the
    #: text: an earlier version pinned a frozen "(212s limit)" constant here and
    #: certified an inert control, because in a real run the clamp moves that
    #: number every call. TestSixIdenticalTimeoutsUnderAMovingClock drives the
    #: real handler with a moving clock; this class keeps the unit-level rules.
    TIMEOUT = {
        "error": "Command timed out. Ask for more time with the `timeout` parameter...",
        "timeout_seconds": 212,
    }

    def test_five_identical_timeouts_are_noted_and_refused(self, tmp_path: Path) -> None:
        """~1060s of a 1200s budget. Clamping makes timeouts MORE frequent, so
        leaving them uncounted pointed the guard away from its best case."""
        guard = _guard()
        actions = []
        for _ in range(6):
            decision = guard.before("exec", self.ARGS, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            if decision is not None and decision.result is not None:
                continue
            guard.after("exec", self.ARGS, self.TIMEOUT, workspace=tmp_path)
        assert actions[2] == "noted", actions
        assert actions[4] == "refused", actions

    def test_a_moving_clamp_does_not_change_that(self, tmp_path: Path) -> None:
        """The limit moves with the budget; the decision may not."""
        guard = _guard()
        actions = []
        for i in range(6):
            decision = guard.before("exec", self.ARGS, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            if decision is not None and decision.result is not None:
                continue
            guard.after(
                "exec", self.ARGS, {**self.TIMEOUT, "timeout_seconds": 212 - i}, workspace=tmp_path
            )
        assert actions[2] == "noted", actions
        assert actions[4] == "refused", actions

    def test_a_different_error_text_resets_the_count(self, tmp_path: Path) -> None:
        """'Retry after a transient failure is not a repeat' survives: only a
        byte-identical error counts."""
        guard = _guard()
        for _ in range(3):
            _run(guard, "exec", self.ARGS, self.TIMEOUT, tmp_path)
        _run(guard, "exec", self.ARGS, {"error": "Command failed: disk full"}, tmp_path)
        assert guard.before("exec", self.ARGS, workspace=tmp_path) is None

    def test_an_errored_read_is_still_never_remembered(self, tmp_path: Path) -> None:
        """Reads keep the old rule: a failed read taught the run nothing, and
        the short circuit would answer with an error it never has to."""
        args = {"path": str(tmp_path / "gone.py")}
        guard = _guard()
        for _ in range(5):
            guard.after("read_file", args, {"error": "Failed to read file"}, workspace=tmp_path)
        assert guard.before("read_file", args, workspace=tmp_path) is None


# ── list_directory cannot be answered from a stat ──────────────────────────


class TestListDirectoryIsNeverAnsweredStale:
    """The review's C3. `target_fingerprint` stats the DIRECTORY, but the
    handler returns each entry's `size`, and with `recursive=True` the whole
    subtree. A file's contents changing does not touch its parent's mtime, so
    the guard told the agent "the content is unchanged" about a listing that was
    out of date — and the loop it broke is exactly "did my deliverable land, and
    how big is it".
    """

    def test_a_grown_file_is_not_hidden_behind_an_unchanged_directory(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "out.txt").write_text("", encoding="utf-8")
        args = {"path": str(workspace)}
        listing = {
            "path": str(workspace),
            "entries": [{"name": "out.txt", "type": "file", "size": 0}],
            "count": 1,
        }
        guard = _guard()
        _run(guard, "list_directory", args, listing, tmp_path)

        (workspace / "out.txt").write_text("x" * 4600, encoding="utf-8")
        decision = guard.before("list_directory", args, workspace=tmp_path)
        assert decision is None or decision.result is None, (
            "the agent was told an out-of-date listing was current"
        )

    def test_it_is_counted_by_output_like_a_search(self) -> None:
        from robothor.engine.repeat_guard import OUTPUT_COUNTED_TOOLS, SHORT_CIRCUIT_TOOLS

        assert "list_directory" not in SHORT_CIRCUIT_TOOLS
        assert "list_directory" in OUTPUT_COUNTED_TOOLS

    def test_a_genuinely_unchanged_listing_is_noted_on_the_third_call(self, tmp_path: Path) -> None:
        args = {"path": str(tmp_path)}
        listing = {"path": str(tmp_path), "entries": [], "count": 0}
        guard = _guard()
        for _ in range(2):
            _run(guard, "list_directory", args, listing, tmp_path)
        decision = guard.before("list_directory", args, workspace=tmp_path)
        assert decision is not None
        assert decision.result is None, "a listing is re-run, never answered from memory"

    def test_a_listing_is_never_refused(self, tmp_path: Path) -> None:
        args = {"path": str(tmp_path)}
        listing = {"path": str(tmp_path), "entries": [], "count": 0}
        guard = _guard()
        actions = []
        for _ in range(9):
            decision = guard.before("list_directory", args, workspace=tmp_path)
            actions.append(None if decision is None else decision.action)
            _run(guard, "list_directory", args, listing, tmp_path)
        assert "refused" not in actions

    def test_read_file_keeps_the_short_circuit(self) -> None:
        """The 6x repeat in the profile was a read_file; the measured value
        stays."""
        from robothor.engine.repeat_guard import SHORT_CIRCUIT_TOOLS

        assert set(SHORT_CIRCUIT_TOOLS) == {"read_file"}


# ── dispatch wiring ────────────────────────────────────────────────────────


class TestTheGuardDoesNotLeakTheSandboxFlag:
    def test_a_short_circuited_call_resets_the_benchmark_context(self, tmp_path: Path) -> None:
        """The guard's early return used to jump over the `finally` that resets
        `set_benchmark_sandbox`, leaving every later CRM write in that task
        silently sandboxed."""
        import asyncio

        import robothor.engine.feature_flags as ff
        import robothor.engine.permissions as perms
        from robothor.crm.dal import _benchmark_sandbox
        from robothor.engine import session_registry
        from robothor.engine.tools import dispatch

        target = tmp_path / "m.py"
        target.write_text("print(1)\n", encoding="utf-8")
        session = _Session()
        session.run_id = "run-sandbox"  # type: ignore[attr-defined]
        session.step_efficiency_mode = "enforce"  # type: ignore[attr-defined]
        real_perm = perms.check_tool_permission
        real_mode = ff.step_efficiency_mode
        perms.check_tool_permission = lambda *a, **kw: None  # type: ignore[assignment]
        ff.step_efficiency_mode = lambda: "enforce"  # type: ignore[assignment]
        session_registry.register(session)  # type: ignore[arg-type]
        try:

            async def _both() -> dict[str, Any]:
                # BOTH calls inside ONE task: a ContextVar leak only shows up
                # in the context the leaking call ran in, and `asyncio.run`
                # would hand the second call a fresh one.
                async def _call() -> dict[str, Any]:
                    return await dispatch._execute_tool(
                        "read_file",
                        {"path": str(target)},
                        run_id="run-sandbox",
                        workspace=str(tmp_path),
                        is_benchmark=True,
                    )

                first = await _call()
                session.record(first)
                assert _benchmark_sandbox.get() is False, "leaked before the guard could"
                second = await _call()
                assert "unchanged_since_step" in second, "the guard did not short-circuit"
                return {"leaked": _benchmark_sandbox.get()}

            assert asyncio.run(_both())["leaked"] is False
        finally:
            session_registry.unregister("run-sandbox")
            perms.check_tool_permission = real_perm  # type: ignore[assignment]
            ff.step_efficiency_mode = real_mode  # type: ignore[assignment]


class TestObserveEvidenceMatchesEnforce:
    def test_the_shadow_line_names_the_step_enforce_would_have_named(self, tmp_path: Path) -> None:
        """Under observe the tool keeps running, so the record was overwritten
        each time and the shadow line drifted to step N-1 while enforce would
        always have said step 1. Observe evidence that understates enforce is
        the wrong direction for a promotion gate."""
        target = tmp_path / "m.py"
        target.write_text("print(1)\n", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard(mode="observe")
        for _ in range(4):
            _run(guard, "read_file", args, {"content": "print(1)\n"}, tmp_path)
        assert guard.reads[next(iter(guard.reads))].step == 1


class TestTheGuardrailRowNamesItsStep:
    def test_step_number_is_recorded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import robothor.engine.tracking as tracking

        rows: list[dict[str, Any]] = []
        monkeypatch.setattr(
            tracking,
            "log_guardrail_event",
            lambda run_id, guardrail_name, action, **kw: rows.append(dict(kw)),
        )
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        guard.run_id = "run-step"
        for _ in range(3):
            _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        guard.before("read_file", args, workspace=tmp_path)
        assert rows
        assert rows[-1]["step_number"] > 0


class TestTheNoteReadsCorrectlyWhereItLands:
    def test_the_tense_matches_the_drain_point(self, tmp_path: Path) -> None:
        """`drain_repeat_notes` puts the note in the conversation AFTER the
        third result, so "about to run a 3rd time" was already false by the
        time the model read it."""
        guard = _guard()
        args = {"command": "python3 x.py"}
        out = {"stdout": "same\n", "exit_code": 1}
        for _ in range(2):
            _run(guard, "exec", args, out, tmp_path)
        decision = guard.before("exec", args, workspace=tmp_path)
        assert decision is not None
        assert "has now run 3 times" in decision.note
        assert "about to run" not in decision.note


# ── The clock must not reach the digest by ANY door ─────────────────────────


class TestNoEngineNumberReachesTheDigest:
    """C5, and it is C1 again through a second door.

    The clamp's `timeout_note` was taken out of the digest, but `_exec` baked
    the CLAMPED seconds into the timeout error text — "Command timed out (5s
    limit)" then "(6s limit)" as the clock moved — and `error` is in the exec
    projection, so the wall clock re-entered the digest anyway. Six identical
    clamped timeouts under a moving clock produced NO note and NO refusal,
    while a frozen clock gave note@3/refusal@5. The round-1 test pinned the
    frozen constant and therefore certified the inert state, exactly as the
    round-0 suite had.

    The rule this class holds: the digest sees the tool's own answer and never
    a number the engine computed for this call.
    """

    def test_the_timeout_error_carries_no_per_call_number(self) -> None:
        import subprocess
        from types import SimpleNamespace

        from robothor.engine.tools.handlers.filesystem import HANDLERS

        real = subprocess.run

        def _timeout(*a: Any, **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="x", timeout=kw.get("timeout", 1))

        subprocess.run = _timeout  # type: ignore[assignment]
        try:
            import asyncio

            first = asyncio.run(
                HANDLERS["exec"](  # type: ignore[arg-type]
                    {"command": "sleep 99", "timeout": 11},
                    SimpleNamespace(workspace="/tmp", run_id=""),
                )
            )
            second = asyncio.run(
                HANDLERS["exec"](  # type: ignore[arg-type]
                    {"command": "sleep 99", "timeout": 12},
                    SimpleNamespace(workspace="/tmp", run_id=""),
                )
            )
        finally:
            subprocess.run = real  # type: ignore[assignment]

        assert first["error"] == second["error"], "the limit is still inside the error text"
        # The number the agent needs is still there — in its own field, which
        # the digest allow-list does not name.
        assert first["timeout_seconds"] == 11
        assert second["timeout_seconds"] == 12

    def test_the_sandboxed_branch_says_the_same_thing(self) -> None:
        """Both branches or neither: a sandboxed agent is the one whose
        `exec` most needs this, and the two were written separately."""
        import pathlib

        import robothor.engine.sandbox as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        assert "Command timed out ({timeout}s limit)" not in body
        assert 'f"Command timed out ({timeout}s limit)"' not in body
        assert '"timeout_seconds": timeout' in body

    def test_two_timeouts_at_different_limits_digest_equal(self) -> None:
        import asyncio
        import subprocess
        from types import SimpleNamespace

        from robothor.engine.repeat_guard import output_digest
        from robothor.engine.tools.handlers.filesystem import HANDLERS

        real = subprocess.run

        def _timeout(*a: Any, **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="x", timeout=kw.get("timeout", 1))

        subprocess.run = _timeout  # type: ignore[assignment]
        try:
            results = [
                asyncio.run(
                    HANDLERS["exec"](  # type: ignore[arg-type]
                        {"command": "sleep 99", "timeout": limit},
                        SimpleNamespace(workspace="/tmp", run_id=""),
                    )
                )
                for limit in (5, 6, 7)
            ]
        finally:
            subprocess.run = real  # type: ignore[assignment]
        digests = {output_digest("exec", r) for r in results}
        assert len(digests) == 1, digests

    def test_any_extra_engine_field_is_invisible_to_the_digest(self) -> None:
        """The general rule, not a list of today's offenders.

        `timeout_note` was the first engine-computed value to reach a result
        and `error`'s embedded limit was the second. The allow-list is what
        stops the third, so it is pinned here directly rather than by naming
        fields as they are discovered.
        """
        from robothor.engine.repeat_guard import output_digest

        base = {"stdout": "same\n", "stderr": "", "exit_code": 1}
        varied = [
            {**base, "timeout_note": f"timeout clamped to {n}s: the run has {n + 30}s left"}
            for n in (5, 6, 7)
        ]
        varied += [{**base, "elapsed_ms": n} for n in (10, 20, 30)]
        varied += [{**base, "pid": n} for n in (111, 222)]
        varied += [{**base, "tmp_path": f"/tmp/run-{n}"} for n in (1, 2)]
        varied += [{**base, "timeout_seconds": n} for n in (5, 6)]
        assert len({output_digest("exec", r) for r in [base, *varied]}) == 1

    def test_what_the_command_actually_said_still_counts(self) -> None:
        """The narrowing must not have made the digest blind."""
        from robothor.engine.repeat_guard import output_digest

        base = {"stdout": "same\n", "stderr": "", "exit_code": 1}
        assert output_digest("exec", base) != output_digest("exec", {**base, "stdout": "other\n"})
        assert output_digest("exec", base) != output_digest("exec", {**base, "exit_code": 0})
        assert output_digest("exec", base) != output_digest("exec", {**base, "stderr": "warn"})
        assert output_digest("exec", {"error": "a"}) != output_digest("exec", {"error": "b"})


class TestSixIdenticalTimeoutsUnderAMovingClock:
    """The round-1 test for I4 fed a frozen error constant straight to the
    guard. This one drives the real `_execute_tool` with a watchdog whose
    clock advances between calls, which is the only shape that would have
    caught C5 — and the only shape a real run has.
    """

    @staticmethod
    def _six(tmp_path: Path, moving: bool) -> Any:
        import asyncio
        import subprocess
        from types import SimpleNamespace

        import robothor.engine.feature_flags as ff
        import robothor.engine.permissions as perms
        from robothor.engine import session_registry
        from robothor.engine.stall_watchdog import _active_watchdog_var
        from robothor.engine.tools import dispatch

        session = _Session()
        session.run_id = f"run-timeout-{moving}"  # type: ignore[attr-defined]
        session.step_efficiency_mode = "enforce"  # type: ignore[attr-defined]
        real_perm = perms.check_tool_permission
        real_mode = ff.step_efficiency_mode
        real_run = subprocess.run

        def _timeout(*a: Any, **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="x", timeout=kw.get("timeout", 1))

        perms.check_tool_permission = lambda *a, **kw: None  # type: ignore[assignment]
        ff.step_efficiency_mode = lambda: "enforce"  # type: ignore[assignment]
        subprocess.run = _timeout  # type: ignore[assignment]
        # 40s left: the clamp bites hard enough that its numbers move every call.
        watchdog = SimpleNamespace(elapsed_seconds=1160.0, _hard_timeout=1200.0)
        token = _active_watchdog_var.set(watchdog)
        session_registry.register(session)  # type: ignore[arg-type]
        try:

            async def _calls() -> list[dict[str, Any]]:
                out = []
                for _ in range(6):
                    res = await dispatch._execute_tool(
                        "exec",
                        {"command": "python3 slow.py", "timeout": 900},
                        run_id=str(session.run_id),  # type: ignore[attr-defined]
                        workspace=str(tmp_path),
                    )
                    session.record(res)
                    out.append(res)
                    if moving:
                        watchdog.elapsed_seconds += 1.0
                return out

            results = asyncio.run(_calls())
        finally:
            _active_watchdog_var.reset(token)
            session_registry.unregister(str(session.run_id))  # type: ignore[attr-defined]
            perms.check_tool_permission = real_perm  # type: ignore[assignment]
            ff.step_efficiency_mode = real_mode  # type: ignore[assignment]
            subprocess.run = real_run  # type: ignore[assignment]
        return session.repeat_guard, results  # type: ignore[attr-defined]

    def test_the_clamp_really_moves_between_calls(self, tmp_path: Path) -> None:
        """Assert the premise first, or the test below proves nothing."""
        _guard_obj, results = self._six(tmp_path, moving=True)
        notes = {r.get("timeout_note") for r in results if r.get("timeout_note")}
        assert len(notes) > 1, notes

    def test_six_identical_timeouts_are_noted_and_refused(self, tmp_path: Path) -> None:
        guard, _results = self._six(tmp_path, moving=True)
        assert guard is not None
        assert guard.counters["exec:noted"] == 1, dict(guard.counters)
        assert guard.counters["exec:refused"] >= 1, dict(guard.counters)

    def test_a_frozen_clock_decides_identically(self, tmp_path: Path) -> None:
        """Whether the wall clock moves may not change a single decision."""
        moving, _ = self._six(tmp_path, moving=True)
        frozen, _ = self._six(tmp_path, moving=False)
        assert dict(moving.counters) == dict(frozen.counters)


class TestARefusalIsNotProgressEither:
    """A refusal is not a failure (C4) — and it is not a success.

    `escalation.record_success()` and `checkpoint.record_success()` both fire on
    any result with no `error` key, so making the refusal error-free moved it
    from one wrong bucket to the other: the escalation manager would read a run
    that had stopped doing anything as recovering, and the checkpoint would
    record progress for a tool call that never ran.
    """

    def test_the_turn_treats_a_refusal_as_neither(self) -> None:
        """Moved to `tool_turn` with the rest of the tool-call block. The
        property is unchanged: a refused call never ran, so it counts as
        neither an error nor a success."""
        import pathlib

        import robothor.engine.tool_turn as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        start = body.index("# ── [ESCALATION] Record error/success ──")
        block = body[start : body.index("iteration_errors.append(", start)]
        assert "refused_by_guard" in block
        assert "elif not refused_by_guard:" in block
        assert "not error_msg and not refused_by_guard" in block

    def test_the_marker_the_runner_keys_on_is_on_every_refusal(self, tmp_path: Path) -> None:
        """The runner reads `repeat_guard`; the guard must always set it."""
        guard = _guard()
        args = {"command": "python3 x.py"}
        out = {"stdout": "same\n", "stderr": "", "exit_code": 1}
        for _ in range(4):
            _run(guard, "exec", args, out, tmp_path)
        decision = guard.before("exec", args, workspace=tmp_path)
        assert decision is not None and decision.result is not None
        assert decision.result["repeat_guard"] == "refused"

    def test_an_answered_read_is_marked_too(self, tmp_path: Path) -> None:
        """A served read did not run the tool either, so it is not progress."""
        target = tmp_path / "m.py"
        target.write_text("x", encoding="utf-8")
        args = {"path": str(target)}
        guard = _guard()
        _run(guard, "read_file", args, {"content": "x"}, tmp_path)
        decision = guard.before("read_file", args, workspace=tmp_path)
        assert decision is not None and decision.result is not None
        assert decision.result["repeat_guard"] == "answered"

    def test_an_ordinary_result_carries_no_marker(self, tmp_path: Path) -> None:
        """So a real success is still a success."""
        import asyncio
        from types import SimpleNamespace

        from robothor.engine.tools.handlers.filesystem import HANDLERS

        result = asyncio.run(
            HANDLERS["exec"](  # type: ignore[arg-type]
                {"command": "echo hi"}, SimpleNamespace(workspace=str(tmp_path), run_id="")
            )
        )
        assert "repeat_guard" not in result
