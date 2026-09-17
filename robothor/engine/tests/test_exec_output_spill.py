"""`exec` output that was cut is PAGINATED, not amputated.

#583 taught the cut to say so: `[truncated: 4000 of 12431 chars shown]`. It
still threw the other 8,431 characters away, which is the half that decides.

MEASURED 2026-09-16, WildClawBench `03_Social_Interaction/task_2`. One listing
call returned twenty records; the handler's 4,000-character slice landed inside
record twelve, and the `total` field — the one value that would have exposed the
cut — sat after the array, in the discarded tail. The agent then read exactly
the twelve records it could see and opened its report with "all 12 of your
recent messages". Every graded item whose evidence was inside the window scored
full marks; every item past the cut scored zero. The extraction was not the weak
part: the input was.

A marker tells the agent something is missing. A spill file lets it get the
missing thing. Only the second one closes this.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robothor.engine.exec_spill import (
    SPILL_DIRNAME,
    STDERR_LIMIT,
    STDOUT_LIMIT,
    prune_run_spills,
    prune_spill_files,
    shape_exec_result,
    spill,
    truncate_stream,
)


def _big(n: int) -> str:
    return "x" * n


class TestShortOutputIsUntouched:
    def test_it_passes_through_verbatim(self, tmp_path: Path) -> None:
        shaped = shape_exec_result(
            {"stdout": "hello\n", "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout"] == "hello\n"

    def test_it_carries_no_flag_and_no_path(self, tmp_path: Path) -> None:
        """A flag on output that lost nothing is a lie in the other direction,
        and it is the one that erodes trust in the flag."""
        shaped = shape_exec_result(
            {"stdout": _big(3_000), "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert "stdout_truncated" not in shaped
        assert "stdout_path" not in shaped
        assert "stdout_chars" not in shaped

    def test_exactly_at_the_limit_does_not_spill(self, tmp_path: Path) -> None:
        text = _big(STDOUT_LIMIT)
        shaped = shape_exec_result(
            {"stdout": text, "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout"] == text
        assert "stdout_path" not in shaped
        assert not (tmp_path / SPILL_DIRNAME).exists()

    def test_one_byte_over_the_limit_does_spill(self, tmp_path: Path) -> None:
        """The boundary is the place a cap is wrong, so it is the place the
        test lives."""
        text = _big(STDOUT_LIMIT + 1)
        shaped = shape_exec_result(
            {"stdout": text, "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout_truncated"] is True
        assert shaped["stdout_chars"] == STDOUT_LIMIT + 1
        assert Path(shaped["stdout_path"]).read_text(encoding="utf-8") == text


class TestTruncatedOutputIsRecoverable:
    def test_the_head_is_the_head_and_the_file_is_the_whole(self, tmp_path: Path) -> None:
        text = "A" + _big(12_000)
        shaped = shape_exec_result(
            {"stdout": text, "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout"].startswith("A" + _big(100))
        assert shaped["stdout_chars"] == len(text)
        assert Path(shaped["stdout_path"]).read_text(encoding="utf-8") == text

    def test_the_marker_names_the_path(self, tmp_path: Path) -> None:
        """ "Some output was cut" is unactionable. Where the rest is, is the
        one sentence that turns a dead end into a next step."""
        shaped = shape_exec_result(
            {"stdout": _big(12_431), "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout_path"] in shaped["stdout"]
        assert f"{STDOUT_LIMIT} of 12431 chars shown" in shaped["stdout"]
        assert "read_file" in shaped["stdout"]

    def test_the_visible_part_stays_bounded(self, tmp_path: Path) -> None:
        """The cap exists to bound context. A marker that made the result
        bigger than the thing it was bounding would be its own defect."""
        shaped = shape_exec_result(
            {"stdout": _big(2_000_000), "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert len(shaped["stdout"]) < STDOUT_LIMIT + 400

    def test_stderr_has_its_own_limit_and_its_own_file(self, tmp_path: Path) -> None:
        err = _big(9_000)
        shaped = shape_exec_result(
            {"stdout": "ok\n", "stderr": err, "exit_code": 1}, workspace=tmp_path
        )
        assert shaped["stderr_truncated"] is True
        assert shaped["stderr_chars"] == len(err)
        assert Path(shaped["stderr_path"]).read_text(encoding="utf-8") == err
        assert "stdout_path" not in shaped
        assert f"{STDERR_LIMIT} of 9000 chars shown" in shaped["stderr"]

    def test_both_streams_spill_to_different_files(self, tmp_path: Path) -> None:
        shaped = shape_exec_result(
            {"stdout": _big(12_000), "stderr": _big(9_000), "exit_code": 1},
            workspace=tmp_path,
        )
        assert shaped["stdout_path"] != shaped["stderr_path"]

    def test_an_undecodable_stream_does_not_raise(self, tmp_path: Path) -> None:
        """`text=True` on a command emitting raw bytes yields lone surrogates.
        A spill that raises there would turn a working command into a tool
        error — the opposite of the trade this makes."""
        text = "\udcff" * 12_000
        shaped = shape_exec_result(
            {"stdout": text, "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout_truncated"] is True
        assert shaped["stdout_chars"] == len(text)

    def test_a_result_without_streams_is_returned_unchanged(self, tmp_path: Path) -> None:
        """A timeout or an infrastructure failure returns `{"error": ...}` and
        has no stream to shape."""
        shaped = shape_exec_result({"error": "Command timed out"}, workspace=tmp_path)
        assert shaped == {"error": "Command timed out"}


class TestTheListingRegression:
    """The measured failure, generic-ised: a count that trails its array.

    The agent cannot know the array was cut, because the field that would have
    told it is itself in the tail. This is the whole task-2 loss in one fixture.
    """

    @staticmethod
    def _listing(records: int) -> str:
        return json.dumps(
            {
                "records": [
                    {"id": f"rec_{i:03d}", "body": "y" * 300} for i in range(1, records + 1)
                ],
                "total": records,
            }
        )

    def test_before_the_spill_the_tail_is_simply_gone(self) -> None:
        payload = self._listing(20)
        head = truncate_stream(payload, STDOUT_LIMIT)
        assert '"total": 20' not in head
        assert "rec_020" not in head

    def test_the_shaped_result_carries_the_total_and_the_path(self, tmp_path: Path) -> None:
        payload = self._listing(20)
        shaped = shape_exec_result(
            {"stdout": payload, "stderr": "", "exit_code": 0}, workspace=tmp_path
        )
        assert shaped["stdout_truncated"] is True
        assert shaped["stdout_chars"] == len(payload)
        recovered = Path(shaped["stdout_path"]).read_text(encoding="utf-8")
        assert json.loads(recovered)["total"] == 20
        assert "rec_020" in recovered


class TestTheSpillStaysInsideTheWorkspace:
    def test_it_lands_under_the_workspace_dot_robothor_tree(self, tmp_path: Path) -> None:
        path = Path(spill(tmp_path, "payload", stream="stdout"))
        assert path.parent == tmp_path / SPILL_DIRNAME
        path.relative_to(tmp_path)  # raises if it escaped

    def test_it_is_never_a_home_path(self, tmp_path: Path) -> None:
        path = Path(spill(tmp_path, "payload", stream="stdout"))
        assert not str(path).startswith(str(Path.home()))

    def test_a_hostile_run_id_cannot_walk_out_of_the_directory(self, tmp_path: Path) -> None:
        """The run id reaches the filename. It comes from the engine, not from
        a model — but a path component is a path component."""
        path = Path(spill(tmp_path, "payload", stream="stdout", run_id="../../etc/x"))
        assert path.parent == tmp_path / SPILL_DIRNAME

    def test_an_unresolvable_workspace_writes_nothing(self, monkeypatch) -> None:
        """No workspace means no answer, and no answer means no file — never
        a fall back to the home directory or the process cwd."""
        import robothor.engine.exec_spill as mod

        monkeypatch.setattr(mod, "_settings_workspace", lambda: None)
        assert spill(None, "payload", stream="stdout") == ""

    def test_a_failed_spill_still_returns_a_usable_result(self, tmp_path: Path) -> None:
        """The marker degrades to #583's wording rather than the call failing:
        an unwritable disk must not turn a working command into a tool error."""
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("", encoding="utf-8")
        shaped = shape_exec_result(
            {"stdout": _big(12_000), "stderr": "", "exit_code": 0}, workspace=blocked
        )
        assert shaped["stdout_truncated"] is True
        assert "stdout_path" not in shaped
        assert "narrower command" in shaped["stdout"]


class TestTheFilesAreReaped:
    def test_a_run_takes_its_own_spills_with_it(self, tmp_path: Path) -> None:
        mine = Path(spill(tmp_path, "a" * 10, stream="stdout", run_id="run-a"))
        theirs = Path(spill(tmp_path, "b" * 10, stream="stdout", run_id="run-b"))
        assert prune_run_spills(tmp_path, "run-a") == 1
        assert not mine.exists()
        assert theirs.exists()

    def test_a_run_that_spilled_nothing_reaps_nothing(self, tmp_path: Path) -> None:
        assert prune_run_spills(tmp_path, "run-a") == 0

    def test_the_sweep_catches_what_a_killed_run_orphaned(self, tmp_path: Path) -> None:
        """A run killed mid-spill never reaches its own cleanup. The retention
        sweep is what stops that from being a permanent leak."""
        orphan = Path(spill(tmp_path, "c" * 10, stream="stdout", run_id="run-dead"))
        removed = prune_spill_files(
            retention_days=7, workspace=tmp_path, now=orphan.stat().st_mtime + 8 * 86400
        )
        assert removed == 1
        assert not orphan.exists()

    def test_a_fresh_file_survives_the_sweep(self, tmp_path: Path) -> None:
        fresh = Path(spill(tmp_path, "d" * 10, stream="stdout", run_id="run-live"))
        assert prune_spill_files(retention_days=7, workspace=tmp_path) == 0
        assert fresh.exists()

    def test_zero_days_disables_the_sweep_rather_than_deleting_everything(
        self, tmp_path: Path
    ) -> None:
        """ "Keep for zero days" is far likelier to be a misconfiguration than
        an instruction — the inversion `vision_batch` was corrected for."""
        kept = Path(spill(tmp_path, "e" * 10, stream="stdout", run_id="run-live"))
        assert prune_spill_files(retention_days=0, workspace=tmp_path, now=1e12) == 0
        assert kept.exists()


class TestTheHandlerUsesIt:
    def test_no_bare_slice_survives_anywhere(self) -> None:
        import inspect

        from robothor.engine import sandbox
        from robothor.engine.tools.handlers import filesystem

        for module in (filesystem, sandbox):
            source = inspect.getsource(module)
            assert "proc.stdout[:4000]" not in source, module.__name__
            assert "proc.stderr[:2000]" not in source, module.__name__

    def test_the_handler_shapes_both_branches(self) -> None:
        import inspect

        from robothor.engine.tools.handlers import filesystem

        source = inspect.getsource(filesystem)
        # The sandboxed branch lost its tail too, and for four releases nobody
        # noticed, because the fix went into the host branch only.
        assert source.count("shape_exec_result(") >= 2


class TestTheLimitsAreNamedConstants:
    def test_both_streams_have_one(self) -> None:
        assert STDOUT_LIMIT > 0
        assert STDERR_LIMIT > 0

    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_a_stream_name_outside_the_pair_is_refused(self, tmp_path: Path, stream: str) -> None:
        """The stream name reaches the filename and the result keys, so it is
        a closed set rather than whatever the caller passed."""
        assert spill(tmp_path, "payload", stream=stream)

    def test_an_unknown_stream_writes_nothing(self, tmp_path: Path) -> None:
        assert spill(tmp_path, "payload", stream="../../etc/passwd") == ""


class TestThePageBackIsNotALoop:
    """The remedy the marker names must not be refused by another control.

    Both of these answer a call before it runs and neither is handed a session,
    so the exemption is by SHAPE — the directory and the filename this module
    writes.
    """

    def test_the_repeat_guard_lets_a_spill_read_through(self) -> None:
        from robothor.engine.repeat_guard import RepeatGuard

        guard = RepeatGuard(run_id="run-1", mode="enforce")
        args = {"path": f"/w/{SPILL_DIRNAME}/run-1__stdout__abc.txt"}
        for _ in range(6):
            assert guard.before("read_file", args, workspace="/w") is None

    def test_an_ordinary_repeated_read_is_still_guarded(self, tmp_path: Path) -> None:
        """The exemption is for the spill and nothing else — a control that
        exempted every read would be the control being deleted."""
        from robothor.engine.repeat_guard import RepeatGuard

        target = tmp_path / "notes.md"
        target.write_text("unchanged", encoding="utf-8")
        guard = RepeatGuard(run_id="run-1", mode="enforce")
        args = {"path": str(target)}
        guard.after("read_file", args, {"content": "unchanged"}, workspace=str(tmp_path))
        assert guard.before("read_file", args, workspace=str(tmp_path)) is not None

    def test_the_no_progress_detector_counts_a_spill_read_as_progress(self) -> None:
        from robothor.engine.scratchpad import Scratchpad

        pad = Scratchpad()
        args = {"path": f"/w/{SPILL_DIRNAME}/run-1__stdout__abc.txt"}
        for _ in range(5):
            pad.record_tool_call("read_file", result={"content": "same"}, tool_input=args)
        assert pad._repeat_count == 0

    def test_an_ordinary_identical_read_still_counts_as_a_repeat(self) -> None:
        from robothor.engine.scratchpad import Scratchpad

        pad = Scratchpad()
        args = {"path": "/w/notes.md"}
        for _ in range(5):
            pad.record_tool_call("read_file", result={"content": "same"}, tool_input=args)
        assert pad._repeat_count == 5


class TestTheSchemaNamesTheCap:
    def test_exec_says_its_output_is_cut_and_where_the_rest_goes(self) -> None:
        """An agent cannot route around a limit it was never told about. For
        one release this description documented the timeout and nothing else
        while a 4,000-character slice decided what the model saw."""
        from robothor.engine.tools.schemas import _CODE_SCHEMAS

        description = _CODE_SCHEMAS["exec"]["function"]["description"]
        assert "4,000" in description
        assert "stdout_path" in description
        assert "read_file" in description

    def test_execute_code_says_the_proxy_keeps_the_whole_response(self) -> None:
        from robothor.engine.tools.schemas import _CODE_SCHEMAS

        description = _CODE_SCHEMAS["execute_code"]["function"]["description"]
        assert "genus_tools" in description
        assert "curl" in description
        assert "stdout_file" in description
