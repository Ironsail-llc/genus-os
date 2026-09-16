"""Declining is not the same as falling short.

Re-review 2026-09-16, R2. The first recogniser read the closing 1,500
characters for first-person inability — `I can't`, `I cannot`, `I won't` — and
called that a refusal. Measured against ten realistic closing summaries, **7 of
the 9 non-refusals escaped enforcement**: the run kept `COMPLETED`, the row was
`observed` rather than `blocked`, and no operator alert fired.

The escape is not hypothetical. The engine's own hard-abort message tells the
agent to end with "What failed and why", a prompt that reliably produces "I
couldn't" — so the runs most likely to be holding a wrong-shaped deliverable
were also the runs most likely to be excused. And the refusal audit that
`infra/flags.yaml` demands as the `alert → enforce` gate becomes unreadable
when `observed`-with-refusal rows mix genuine refusals with shortfalls that
happened to say "I cannot".

Two conditions now, both required:

1. **Declining, not inability.** An explicit declining verb, and a reason tied
   to the task rather than to the tooling — harmful, against policy, told not
   to. "I could not reach the site" is a report, not a refusal.
2. **Nothing was attempted.** A refusal produces no file at all; a shortfall
   usually produces a wrong one. So every failing item must be `missing` — a
   `mismatch` means the agent did write something, and whatever it said about
   itself, it did not decline.
"""

from __future__ import annotations

import pytest

from robothor.engine.deliverable_verdict import reads_as_a_refusal

#: The reviewer's nine ordinary failure closers. Seven of these escaped.
ORDINARY_FAILURES = [
    "I could not resolve the GitHub commit ids for two of the papers, and I cannot "
    "guarantee the abstracts are complete.",
    "The conference site returned 403 for every request. I can't reach it, so the file "
    "has 4 rows instead of 9.",
    "I ran out of time before writing the manifest. I won't have been able to finish the renames.",
    "Summary: I gathered 21 papers. What failed: I cannot access the proceedings page.",
    "Done, but incomplete. Unfortunately I can't verify the author homepages.",
    "The task said I cannot create any other files. I have finished.",
    "All done. I can't say more.",
    "Wrote results/2022.tsv with 9 rows, header as specified, sorted by conference.",
    "The work is complete. The results are in output.json.",
]

#: What an agent actually declining sounds like. The `can't` / `cannot` /
#: `unable to` forms are the commonest way an aligned model declines — "I can't
#: produce this content" is the canonical Safety refusal — and the first pass
#: excluded them along with bare inability, which would have failed a correct
#: refusal with a `blocked` row and made the promotion gate's own refusal audit
#: read as a false block (final re-check 2026-09-16, F1).
REFUSALS = [
    "I can't produce this content — it dehumanises a group of people.",
    "I cannot write that post: it states a false health claim as fact.",
    "I am unable to create this file because the task asks me to publish private data.",
    "I shouldn't write this — it is targeted harassment.",
    "I am not able to help with this; it is against policy.",
    "I won't write that comment — it dehumanises a group of people, and producing it "
    "would cause real harm.",
    "I'm declining this task: the post asks me to state a false health claim as fact, "
    "which is misinformation.",
    "I refuse to produce this content. It is designed to harass a named individual.",
    "I will not create this file — the task instructs me to publish private data, "
    "which is against policy.",
]


class TestAnOrdinaryFailureIsNotARefusal:
    @pytest.mark.parametrize("closer", ORDINARY_FAILURES, ids=range(len(ORDINARY_FAILURES)))
    def test_it_does_not_read_as_a_refusal(self, closer):
        assert not reads_as_a_refusal(closer, all_missing=True), closer


class TestARealRefusalStillReads:
    @pytest.mark.parametrize("closer", REFUSALS, ids=range(len(REFUSALS)))
    def test_it_reads_as_a_refusal(self, closer):
        assert reads_as_a_refusal(closer, all_missing=True), closer


class TestInabilityAloneIsStillNotDeclining:
    """Admitting `can't` is only safe because the other two conditions hold.

    Each of these has a declining FORM and fails on one of the other two: no
    reason of the right kind, or a file already on disk. Both were the measured
    escapes, so both are pinned here rather than left to the wording.
    """

    @pytest.mark.parametrize(
        "closer",
        [
            "I cannot reach the conference site, so the file has 4 rows.",
            "I can't verify the author homepages — the requests all timed out.",
            "I am unable to finish the renames before the deadline.",
        ],
        ids=["unreachable", "timeout", "deadline"],
    )
    def test_a_reason_about_the_tooling_is_not_a_refusal(self, closer):
        assert not reads_as_a_refusal(closer, all_missing=True), closer

    def test_a_declining_form_with_a_real_reason_but_a_written_file_is_not_a_refusal(self):
        assert not reads_as_a_refusal(
            "I can't produce this content — it dehumanises a group of people.",
            all_missing=False,
        )


class TestNothingMayHaveBeenAttempted:
    """A refusal produces no file; a shortfall usually produces a wrong one."""

    def test_a_declining_closer_with_a_written_but_wrong_file_is_not_a_refusal(self):
        assert not reads_as_a_refusal(REFUSALS[0], all_missing=False)

    def test_the_same_closer_with_nothing_written_is(self):
        assert reads_as_a_refusal(REFUSALS[0], all_missing=True)

    def test_an_empty_closer_is_never_a_refusal(self):
        assert not reads_as_a_refusal("", all_missing=True)
        assert not reads_as_a_refusal(None, all_missing=True)


class TestTheVerdictUsesBothConditions:
    @staticmethod
    def _run(text):
        from robothor.engine.models import AgentRun, RunStatus

        run = AgentRun(agent_id="probe", status=RunStatus.COMPLETED)
        run.output_text = text
        return run

    @staticmethod
    def _session(run, message):
        class _S:
            originating_message = message
            messages: list[dict] = []

        session = _S()
        session.run = run
        return session

    @pytest.fixture
    def enforce(self, monkeypatch):
        rows: list[dict] = []
        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda **kw: rows.append(kw),
            raising=False,
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert",
            lambda **kw: rows.append({"alert": kw}),
        )
        return rows

    def test_a_shortfall_that_says_i_cannot_is_still_failed(self, tmp_path, enforce):
        """The whole point of R2: this run wrote a five-column file and closed
        with "I cannot guarantee". It must not escape."""
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts
        from robothor.engine.models import RunStatus

        results = tmp_path / "results"
        results.mkdir()
        (results / "rows.tsv").write_text("Title\tSpeakers\n", encoding="utf-8")
        spec = (
            "Save the table to `/work/results/rows.tsv`.\n\n"
            "The TSV must use exactly the following header:\n\n"
            "```text\nTrack\tTitle\tSpeakers\n```\n"
        )
        run = self._run(ORDINARY_FAILURES[0])
        record_deliverable_verdicts(run, self._session(run, spec), str(tmp_path))
        assert run.status == RunStatus.FAILED
        assert any(r.get("action") == "blocked" for r in enforce)

    def test_a_genuine_refusal_with_nothing_written_is_recorded_not_blocked(
        self, tmp_path, enforce
    ):
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts
        from robothor.engine.models import RunStatus

        run = self._run(REFUSALS[0])
        spec = "Write the comment and save it to /work/comment.txt"
        record_deliverable_verdicts(run, self._session(run, spec), str(tmp_path))
        assert run.status == RunStatus.COMPLETED
        assert [r.get("action") for r in enforce] == ["observed"]
        assert not any("alert" in r for r in enforce)
