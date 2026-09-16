"""A placeholder in a path is a pattern, never a filename.

Re-review 2026-09-16, R1 — and a regression the Chinese-coverage work
introduced, which makes it the first false positive the 60-spec corpus has
produced in two rounds.

`01_Productivity_Flow_task_9` says *"For each page, create
`/tmp_workspace/results/scp-XXX/`"* and then *"save it as `text.md`"*. `XXX` is
the spec's own stand-in for the page number: a correct run writes
`scp-173/text.md`, `scp-096/text.md`, … and **never** `scp-XXX/text.md`. As a
literal `PathItem` it is unsatisfiable by construction, so at `enforce` — the
rung the bench container defaults to — a perfectly correct run is nagged
mid-run, re-asked at the end, and then failed.

The fix is not silence. A template segment says exactly what the contract is:
this directory, this extension, one file per thing. `PatternItem` checks that
and nothing more, so the real failure — nothing written, or one flat file where
the spec asked for a directory per page — is still caught, while no correct run
is ever asked to create a filename the spec never meant literally.
"""

from __future__ import annotations

import pytest

from robothor.engine.deliverable_contract import (
    STATUS_MISSING,
    STATUS_OK,
    PathItem,
    PatternItem,
    check_contract,
    extract_contract,
)

TASK_9_SHAPED = (
    "Crawl the pages and save the results.\n\n"
    "2. For each page, create `/tmp_workspace/results/scp-XXX/`.\n"
    "3. Extract the main article text and save it as "
    "`/tmp_workspace/results/scp-XXX/text.md`.\n"
)


class TestATemplateSegmentIsNotAFilename:
    @pytest.mark.parametrize(
        "text",
        [
            "For each page, save it as /tmp_workspace/results/scp-XXX/text.md",
            "Save the output to results/NNN/out.tsv.",
            "Save the daily file to results/YYYY-MM-DD.md.",
            "Write each row to out/ZZZ.csv",
        ],
        ids=["scp-XXX", "NNN", "YYYY-MM-DD", "ZZZ"],
    )
    def test_no_literal_path_item_is_produced(self, text):
        assert [i.path for i in extract_contract(text).items if isinstance(i, PathItem)] == [], text

    @pytest.mark.parametrize(
        "text",
        [
            "Save each record to results/<id>.json.",
            "Save each row to results/{name}.csv.",
        ],
        ids=["angle", "brace"],
    )
    def test_bracketed_placeholders_stay_silent(self, text):
        """These were already excluded, but only by the path charset — by
        accident rather than by rule. Pinned so the next charset change cannot
        quietly turn them into demands."""
        assert extract_contract(text).items == (), text

    @pytest.mark.parametrize(
        "text",
        [
            "Save the table to results/2022.tsv",
            "Save the paper list to results/AAAI.tsv",
            "Write the digest to results/README.md",
            "Save it to results/UPPER.json",
        ],
        ids=["digits", "conference-acronym", "readme", "all-caps-word"],
    )
    def test_a_real_name_is_still_literal(self, text):
        """`AAAI` contains three identical letters and is a real conference.
        A template run has to be the whole segment or delimited on both sides,
        or every acronym becomes a wildcard."""
        assert [i.path for i in extract_contract(text).items if isinstance(i, PathItem)], text


class TestThePatternSaysWhatTheContractActuallyIs:
    def test_a_pattern_item_is_produced(self):
        patterns = [i for i in extract_contract(TASK_9_SHAPED).items if isinstance(i, PatternItem)]
        assert len(patterns) == 1
        assert patterns[0].pattern == "results/scp-*/text.md"

    def test_the_literal_prefix_survives(self):
        """`scp-*` and not `*`: the spec did say the directories are named for
        the item, and a pattern that matched anything would certify a run that
        wrote its own layout."""
        assert (
            "scp-"
            in next(
                i for i in extract_contract(TASK_9_SHAPED).items if isinstance(i, PatternItem)
            ).pattern
        )

    def test_it_carries_the_sentence_it_came_from(self):
        item = next(i for i in extract_contract(TASK_9_SHAPED).items if isinstance(i, PatternItem))
        assert "scp-XXX" in item.evidence


class TestACorrectTaskNineWorkspaceIsOk:
    @staticmethod
    def _workspace(tmp_path, ids=("scp-001", "scp-017", "scp-173")):
        results = tmp_path / "results"
        results.mkdir()
        for name in ids:
            item = results / name
            item.mkdir()
            (item / "text.md").write_text(f"# {name}\n\nbody\n", encoding="utf-8")
        return tmp_path

    def test_it_is_satisfied(self, tmp_path):
        report = check_contract(extract_contract(TASK_9_SHAPED), self._workspace(tmp_path))
        assert report.satisfied, report.message

    def test_every_pattern_finding_is_ok(self, tmp_path):
        findings = check_contract(
            extract_contract(TASK_9_SHAPED), self._workspace(tmp_path)
        ).findings
        assert [f.status for f in findings if isinstance(f.item, PatternItem)] == [STATUS_OK]

    def test_nothing_written_is_still_caught(self, tmp_path):
        """The pattern is not a licence to write nothing. This is the failure
        the item exists for."""
        (tmp_path / "results").mkdir()
        report = check_contract(extract_contract(TASK_9_SHAPED), tmp_path)
        assert not report.satisfied
        assert "results/scp-*/text.md" in report.message

    def test_one_flat_file_where_a_directory_per_item_was_asked_for(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "text.md").write_text("everything in one file", encoding="utf-8")
        report = check_contract(extract_contract(TASK_9_SHAPED), tmp_path)
        assert not report.satisfied

    def test_an_empty_match_does_not_count(self, tmp_path):
        """Same rule the rest of this module holds: a touched path is not a
        produced deliverable."""
        results = tmp_path / "results"
        (results / "scp-001").mkdir(parents=True)
        (results / "scp-001" / "text.md").write_text("", encoding="utf-8")
        findings = check_contract(extract_contract(TASK_9_SHAPED), tmp_path).findings
        assert [f.status for f in findings if isinstance(f.item, PatternItem)] == [STATUS_MISSING]


class TestTheRealSpecIsPinned:
    """The corpus is what caught this, so the corpus is what keeps it caught."""

    def test_task_9_produces_no_unsatisfiable_literal(self):
        from bench.wildclaw import corpus

        tasks = corpus.tasks_dir()
        if tasks is None:
            pytest.skip("benchmark checkout not present")
        matches = list(tasks.rglob("*task_9_scp_crawl*.md"))
        if not matches:
            pytest.skip("task_9 not in this checkout")
        body = matches[0].read_text(encoding="utf-8", errors="replace")
        body = body.split("---", 2)[-1].split("\n## Grading Criteria", 1)[0]
        items = extract_contract(body).items
        literals = [i.path for i in items if isinstance(i, PathItem)]
        assert not any("XXX" in p for p in literals), literals
        # And the sentence is not simply dropped: it still states a contract.
        assert any(isinstance(i, PatternItem) and "scp-" in i.pattern for i in items), [
            type(i).__name__ for i in items
        ]
