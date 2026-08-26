"""The engine knows what file the task asked for, and whether it exists.

Measured, repeatedly, on WildClawBench: a task says "save the result to
/tmp_workspace/results/result.png"; the grader awards unconditional credit
for that file merely existing; and our agent writes SEVEN images into
`results/` under other names. The string `result.png` appears exactly once
in the whole transcript — in the prompt. Elsewhere an agent printed the full
answer to stdout at turn 323 and spent its last four calls re-deriving it
instead of writing the two lines the task asked for.

The engine had no way to notice. `verify_output` judges the agent's
NARRATION, never the workspace, and the deadline note says "write your
partial answer" without knowing where.

So: read the paths the task itself names, and when the clock is running down
and one of them is not on disk, say so by name. This is not benchmark
coaching — production tasks name deliverables constantly ("write the report
to X", "save the CSV to Y"), and an agent that says "done" with no file
there is the completion-contract failure this platform already exists to
catch.

Deliberately conservative: a false positive nags an agent about a file it
was never asked for, and a nagging control gets ignored.
"""

from __future__ import annotations

from robothor.engine.deliverables import (
    deadline_note,
    declared_paths,
    missing_deliverables_note,
)


class TestReadingWhatTheTaskAsksFor:
    def test_a_plain_absolute_path(self):
        assert "/tmp_workspace/results/result.png" in declared_paths(
            "Save the finished image to /tmp_workspace/results/result.png"
        )

    def test_a_backticked_path(self):
        assert "/out/report.md" in declared_paths("Write your report to `/out/report.md`.")

    def test_several_paths(self):
        found = declared_paths(
            "Save the image to /ws/results/result.png and the description "
            "to /ws/results/description.txt"
        )
        assert found == ["/ws/results/result.png", "/ws/results/description.txt"]

    def test_order_is_preserved_and_duplicates_collapse(self):
        found = declared_paths("write /a/x.md then check /a/x.md then /a/y.md")
        assert found == ["/a/x.md", "/a/y.md"]

    def test_a_path_with_no_extension_is_ignored(self):
        """A directory or a bare mention is not a deliverable."""
        assert declared_paths("work inside /tmp_workspace and /usr/bin") == []

    def test_prose_without_paths_yields_nothing(self):
        assert declared_paths("Summarise the findings and explain them.") == []

    def test_input_paths_are_not_deliverables(self):
        """The task's INPUT file is not something the agent must produce."""
        found = declared_paths(
            "The input image is at /ws/input/origin.png. Save your answer to /ws/results/result.png"
        )
        assert "/ws/results/result.png" in found
        assert "/ws/input/origin.png" not in found

    def test_urls_are_not_paths(self):
        assert declared_paths("fetch https://example.com/a/b.json") == []

    def test_it_is_bounded(self):
        many = " ".join(f"/ws/results/f{i}.md" for i in range(500))
        assert len(declared_paths(many)) <= 10

    def test_empty_and_none_are_safe(self):
        assert declared_paths("") == []
        assert declared_paths(None) == []


class TestTheNote:
    def test_names_a_missing_deliverable(self, tmp_path):
        note = missing_deliverables_note(
            [str(tmp_path / "results" / "result.png")], remaining=120, workspace=str(tmp_path)
        )
        assert note is not None
        assert "result.png" in note

    def test_says_nothing_when_the_file_exists(self, tmp_path):
        p = tmp_path / "out.md"
        p.write_text("done", encoding="utf-8")
        assert missing_deliverables_note([str(p)], remaining=120, workspace=str(tmp_path)) is None

    def test_says_nothing_with_no_declared_paths(self):
        assert missing_deliverables_note([], remaining=120, workspace="/tmp") is None

    def test_reports_only_the_missing_ones(self, tmp_path):
        there = tmp_path / "a.md"
        there.write_text("x", encoding="utf-8")
        gone = tmp_path / "b.md"
        note = missing_deliverables_note(
            [str(there), str(gone)], remaining=90, workspace=str(tmp_path)
        )
        assert "b.md" in note
        assert "a.md" not in note

    def test_the_note_states_the_time_left(self, tmp_path):
        note = missing_deliverables_note(
            [str(tmp_path / "x.md")], remaining=90, workspace=str(tmp_path)
        )
        assert "90" in note

    def test_an_unreadable_path_does_not_raise(self):
        missing_deliverables_note(["\x00bad"], remaining=10, workspace="/tmp")


class TestTheRunnerChecksThem:
    @staticmethod
    def _source() -> str:
        from pathlib import Path

        import robothor.engine.runner as m

        return Path(m.__file__).read_text(encoding="utf-8")

    def test_the_deadline_path_consults_deliverables(self):
        body = self._source()
        assert "deadline_note(" in body, (
            "the deadline warning still cannot say WHICH file is missing"
        )


class TestTheComposedNote:
    def test_it_carries_both_halves(self, tmp_path):
        note = deadline_note(
            90.0, 100.0, f"save it to {tmp_path}/results/out.md", workspace=str(tmp_path)
        )
        assert note is not None
        assert "Time budget" in note
        assert "out.md" in note

    def test_no_warning_before_the_threshold(self, tmp_path):
        assert (
            deadline_note(10.0, 100.0, f"save to {tmp_path}/x.md", workspace=str(tmp_path)) is None
        )

    def test_the_warning_stands_alone_when_nothing_was_declared(self):
        note = deadline_note(90.0, 100.0, "summarise the findings", workspace="/tmp")
        assert note is not None and "Time budget" in note


class TestPathsAreConfinedToTheWorkspace:
    """Prompt text is untrusted input, and these paths reach the filesystem.

    `declared_paths` reads whatever the task says; `missing_deliverables_note`
    then stats it. A prompt is attacker-influenceable in any deployment where
    someone else can file a task, so a path from one must not send the engine
    poking at arbitrary locations — CodeQL flags exactly this shape
    (py/path-injection), and it is right to.

    Confinement is also the more CORRECT rule: a file outside the agent's
    workspace is not the deliverable the task is graded on.
    """

    def test_a_path_outside_the_workspace_is_ignored(self, tmp_path):
        note = missing_deliverables_note(
            ["/etc/shadow", str(tmp_path / "out.md")], remaining=60, workspace=str(tmp_path)
        )
        assert note is not None
        assert "/etc/shadow" not in note
        assert "out.md" in note

    def test_traversal_out_of_the_workspace_is_ignored(self, tmp_path):
        note = missing_deliverables_note(
            [str(tmp_path / ".." / ".." / "etc" / "passwd")],
            remaining=60,
            workspace=str(tmp_path),
        )
        assert note is None

    def test_no_workspace_means_no_filesystem_access(self, tmp_path):
        """Fail closed: without a workspace to confine to, check nothing."""
        assert missing_deliverables_note([str(tmp_path / "x.md")], remaining=60) is None

    def test_the_composed_note_passes_the_workspace_through(self, tmp_path):
        note = deadline_note(90.0, 100.0, f"save it to {tmp_path}/out.md", workspace=str(tmp_path))
        assert note is not None and "out.md" in note


class TestTheExtractorCannotBeHung:
    """The path pattern runs over untrusted task text.

    The first version nested quantifiers — `(?:[\\w.\\-]+/)+` — which
    backtracks polynomially on input like `/-/-/-/-/...`. CodeQL flagged it
    (py/polynomial-redos) and was right: a crafted task prompt could hang the
    engine before the agent ran a single step. The pattern is now a single
    flat quantifier, which is linear.
    """

    def test_a_pathological_string_returns_promptly(self):
        import time

        evil = "/" + "-/" * 4000
        started = time.perf_counter()
        declared_paths(evil)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"the extractor took {elapsed:.1f}s on crafted input"

    def test_a_long_benign_string_is_also_fast(self):
        import time

        text = " ".join(f"/ws/results/file{i}.md" for i in range(5000))
        started = time.perf_counter()
        declared_paths(text)
        assert time.perf_counter() - started < 1.0
