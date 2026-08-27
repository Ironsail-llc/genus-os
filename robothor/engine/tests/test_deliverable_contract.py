"""A task that names its output file must produce THAT file.

2026-08-26, WildClaw task_4: the agent did the research correctly --
verified 7 of 9 author homepages with live HTTP 200s -- then wrote
``/tmp_workspace/results/summary.md`` when the spec said "save them to
/tmp_workspace/results/2022.tsv". It scored 0.00 on all eight criteria,
including ``output_exists``, after burning 3.4M tokens. That single task
carries -0.87 of the -1.04 total gap against OpenClaw; 7 of 10 tasks are
at parity.

``run_verification.verify_run`` cannot catch this. It matches CLAIMS in
the final message against TOOL EVIDENCE in the trace, so "I saved the
results" plus any file_write event verifies clean. Nothing anywhere
compares the work to the deliverable the TASK named.

This is deliberately a general capability, not a bench fix. Editing the
benchmark agent's prompt would be teaching to the test -- the exact error
recorded in peak-performance-campaign-2026-08-21, where a calibration
skill taught agents to widen a regex.
"""

from __future__ import annotations

from robothor.engine.deliverable_contract import (
    check_deliverables,
    required_deliverables,
)


class TestExtractingTheContract:
    def test_save_them_to_a_named_file(self):
        task = "Research the papers and save them to /tmp_workspace/results/2022.tsv"
        assert required_deliverables(task) == ["/tmp_workspace/results/2022.tsv"]

    def test_write_to_a_named_file(self):
        task = "Summarise the findings and write the output to reports/summary.csv"
        assert required_deliverables(task) == ["reports/summary.csv"]

    def test_save_as_phrasing(self):
        assert required_deliverables("Save as output/final.json") == ["output/final.json"]

    def test_multiple_deliverables_are_all_captured(self):
        task = "Save them to results/a.tsv and also write the log to results/b.log"
        assert set(required_deliverables(task)) == {"results/a.tsv", "results/b.log"}

    def test_the_same_path_twice_is_listed_once(self):
        task = "Write to out/x.csv. Remember: out/x.csv must be tab separated."
        assert required_deliverables(task) == ["out/x.csv"]


class TestNotOverReaching:
    """False positives would nag agents about files that are not deliverables."""

    def test_a_path_that_is_only_read_is_not_a_deliverable(self):
        assert required_deliverables("Read the data from input/source.csv") == []

    def test_prose_with_no_path_yields_nothing(self):
        assert required_deliverables("Summarise the 2022 conference papers") == []

    def test_a_bare_extension_is_not_a_path(self):
        assert required_deliverables("Prefer .tsv over .csv formatting") == []

    def test_a_url_is_not_a_deliverable(self):
        task = "Save them to the sheet at https://example.com/results/2022.tsv"
        assert required_deliverables(task) == []

    def test_an_empty_task_is_safe(self):
        assert required_deliverables("") == []
        assert required_deliverables(None) == []  # type: ignore[arg-type]


class TestCheckingTheContract:
    def test_a_missing_deliverable_is_reported(self, tmp_path):
        target = tmp_path / "2022.tsv"
        report = check_deliverables([str(target)])
        assert not report.satisfied
        assert report.missing == [str(target)]

    def test_a_present_deliverable_passes(self, tmp_path):
        target = tmp_path / "2022.tsv"
        target.write_text("a\tb\n")
        report = check_deliverables([str(target)])
        assert report.satisfied
        assert report.missing == []

    def test_an_empty_file_does_not_satisfy_the_contract(self, tmp_path):
        """Touching the path is not producing the deliverable."""
        target = tmp_path / "2022.tsv"
        target.write_text("")
        report = check_deliverables([str(target)])
        assert not report.satisfied

    def test_no_contract_is_satisfied_vacuously(self):
        report = check_deliverables([])
        assert report.satisfied
        assert report.missing == []

    def test_the_report_names_what_to_do(self, tmp_path):
        target = tmp_path / "2022.tsv"
        report = check_deliverables([str(target)])
        assert "2022.tsv" in report.message
        assert report.message.strip() != ""


class TestRunLevelWiring:
    def test_a_run_with_no_task_requires_nothing(self):
        from robothor.engine.deliverable_contract import check_run_deliverables

        class _Run:
            task_id = None
            tenant_id = "t"

        assert check_run_deliverables(_Run()) is None

    def test_a_task_naming_no_path_yields_no_verdict(self, monkeypatch):
        """None, not 'satisfied' — a vacuous pass on every run is noise."""
        import robothor.engine.deliverable_contract as dc

        monkeypatch.setattr(dc, "task_text_for_run", lambda run, session=None: "Summarise the papers")
        assert dc.check_run_deliverables(object()) is None

    def test_a_named_but_absent_deliverable_fails(self, monkeypatch, tmp_path):
        import robothor.engine.deliverable_contract as dc

        target = tmp_path / "2022.tsv"
        monkeypatch.setattr(
            dc, "task_text_for_run", lambda run, session=None: f"Research and save them to {target}"
        )
        report = dc.check_run_deliverables(object())
        assert report is not None and not report.satisfied

    def test_an_unreadable_task_never_raises(self, monkeypatch):
        import robothor.engine.deliverable_contract as dc

        class _Run:
            task_id = "t1"
            tenant_id = "x"

        monkeypatch.setattr(
            "robothor.crm.dal.get_task",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        assert dc.task_text_for_run(_Run()) == ""
