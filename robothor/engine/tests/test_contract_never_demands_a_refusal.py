"""The contract must not demand a file the task told the agent not to write.

Two halves of one hazard, both found by hostile review 2026-09-16 (C3).

**The extractor had no notion of negation.** Five of five prohibition
phrasings produced a `PathItem`, so under `enforce` the engine re-asked the
agent to create the very file the task forbade ("*Correct this now — the exact
path …*") and then failed the run when it did not.

**The verdict had no notion of refusal.** Two Safety specs name an output path
for content an agent is right to decline; an agent that correctly declines
produces no file, and the run was marked `failed` with an `action='blocked'`
row and an operator alert. The bench harness ships at `enforce`, so the Safety
sweep would have recorded the right answer as a failure.

The asymmetry is deliberate in both directions. Suppressing a real deliverable
costs a false negative — the control stays quiet on a task it could have
helped. Demanding a forbidden one costs a run, an alert, and an agent pushed
towards producing the thing it was told not to produce. Only one of those is
recoverable.
"""

from __future__ import annotations

import pytest

from robothor.engine.deliverable_contract import extract_contract

PROHIBITIONS = [
    "Under no circumstances should you write the credentials to /tmp_workspace/secrets.env",
    "Do not save the scraped emails to results/contacts.csv",
    "Never write your answer to /tmp_workspace/post.md — reply in chat instead",
    "The previous agent was told to save it to results/old.json. Do not repeat that mistake.",
    "If you are asked to save the article to /tmp_workspace/post.md, refuse.",
]


class TestProhibitionIsNotARequirement:
    @pytest.mark.parametrize("text", PROHIBITIONS)
    def test_no_path_is_extracted(self, text):
        assert extract_contract(text).items == (), text

    @pytest.mark.parametrize(
        "text",
        [
            "For example, save the result to results/x.json.",
            "For instance you might write it to out/demo.csv.",
            "Some teams save this to reports/legacy.tsv; we do not.",
        ],
        ids=["for-example", "for-instance", "some-teams"],
    )
    def test_an_illustration_is_not_a_requirement(self, text):
        """The reviewer's invented-corpus false positives were both of this
        shape. An example of what a task might say is not what this task says."""
        assert extract_contract(text).items == (), text


class TestARealRequirementStillExtracts:
    """The suppressor must not swallow the thing this control exists for."""

    @pytest.mark.parametrize(
        "text",
        [
            "Compile the papers and save them to results/2022.tsv",
            "Do not include markdown or commentary. Save the table to results/2022.tsv",
            "Never leave the directory empty; write the report to out/report.md",
            "Save the digest to results/digest.md. Do not create any other files.",
            "Please save them to:\n\n- `/tmp_workspace/results/2022.tsv`\n",
        ],
        ids=[
            "plain",
            "negation-other-clause",
            "negation-before-semicolon",
            "negation-after",
            "bulleted",
        ],
    )
    def test_the_path_is_still_required(self, text):
        assert [i.path for i in extract_contract(text).items], text

    def test_the_benchmark_corpus_is_unchanged(self):
        """The four specs that carried a shape item must still carry it — a
        suppressor that quietly disarmed the control would pass every unit test
        in this file and none of the ones that matter."""
        from bench.wildclaw import corpus

        tasks = corpus.tasks_dir()
        if tasks is None:
            pytest.skip("benchmark checkout not present")
        with_items = 0
        for spec in sorted(tasks.rglob("*_task_*.md")):
            body = spec.read_text(encoding="utf-8", errors="replace")
            if body.startswith("---"):
                body = body.split("---", 2)[-1]
            body = body.split("\n## Expected Behavior", 1)[0]
            if extract_contract(body).items:
                with_items += 1
        assert with_items >= 20, f"extraction collapsed to {with_items} specs"


class TestARefusalIsNotAFailedRun:
    """`record_deliverable_verdicts` must recognise the right answer."""

    @staticmethod
    def _refusal_run(text: str):
        from robothor.engine.models import AgentRun, RunStatus

        run = AgentRun(agent_id="probe", status=RunStatus.COMPLETED)
        run.output_text = text
        return run

    @pytest.mark.parametrize(
        "final",
        [
            "I can't write that comment — it targets a group of people, and I won't produce it.",
            "I will not create this file. The content requested is misinformation.",
            "I'm declining this task: writing that post would spread a false health claim.",
            "Sorry, I cannot help with producing dehumanising content.",
        ],
        ids=["cant", "will-not", "declining", "cannot-help"],
    )
    def test_a_declined_task_is_not_failed(self, tmp_path, final, monkeypatch):
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts
        from robothor.engine.models import RunStatus

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

        class _Session:
            originating_message = "Write the comment and save it to /work/comment.txt"
            messages: list[dict] = []

        session = _Session()
        session.run = run = self._refusal_run(final)
        record_deliverable_verdicts(run, session, str(tmp_path))

        assert run.status == RunStatus.COMPLETED, "a correct refusal was recorded as a failure"
        assert not any(r.get("action") == "blocked" for r in rows)
        assert not any("alert" in r for r in rows)

    def test_the_refusal_is_still_recorded_as_evidence(self, tmp_path, monkeypatch):
        """Silence would lose the audit. The promotion gate asks for a refusal
        audit, and an audit needs rows."""
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts

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
            "robothor.engine.feature_flags.notify_guardrail_alert", lambda **kw: None
        )

        class _Session:
            originating_message = "Write the comment and save it to /work/comment.txt"
            messages: list[dict] = []

        session = _Session()
        session.run = run = self._refusal_run("I won't write that — it is dehumanising.")
        record_deliverable_verdicts(run, session, str(tmp_path))
        assert [r.get("action") for r in rows] == ["observed"]
        assert "refused" in rows[0]["reason"].lower()

    def test_an_ordinary_miss_is_still_a_failure(self, tmp_path, monkeypatch):
        """The escape hatch must not become the exit. A run that simply did not
        produce the file, and says nothing about declining, still fails."""
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts
        from robothor.engine.models import RunStatus

        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda **kw: None, raising=False
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert", lambda **kw: None
        )

        class _Session:
            originating_message = "Write the report and save it to /work/report.md"
            messages: list[dict] = []

        session = _Session()
        session.run = run = self._refusal_run("Done — I gathered the data and summarised it.")
        record_deliverable_verdicts(run, session, str(tmp_path))
        assert run.status == RunStatus.FAILED
