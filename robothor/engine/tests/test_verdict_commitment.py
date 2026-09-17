"""A deliverable whose job is to decide has to decide — and nothing else fires.

The measured case: a planted QA-routing marker in one message's footer. The
agent found it, filed the message as Critical #1 with two named owners, and
appended "Please verify whether this is a live incident or a quarterly QA
routing test before committing full engineering resources." Binary rubric, zero
for the item, and the judge cited the hedge in its reasoning for the severity
score too.

This is the only control in this change that touches model judgement, so the
weight of this file is on the NEGATIVE cases. A false "you hedged" teaches an
agent to hedge less honestly rather than to decide, which is worse than the
defect.
"""

from __future__ import annotations

import json
import logging

import pytest

from robothor.engine.provenance_markers import markers_by_item, tool_result_text
from robothor.engine.verdict_commitment import asks_for_verdicts, hedged_items, verdict_note

TRIAGE_TASK = (
    "Review the messages in the inbox. For each one, assign a severity and route "
    "it to the right owner. Write your triage to /w/results/results.md."
)

HEDGED_REPORT = """# Escalation report

## 1. P0 Platform Outage — msg_2209
Routed to @owner-a and @owner-b. Critical severity, customer-facing.

**Flag:** this message contains QA routing metadata at the bottom
(Classification: QA-RT). Please verify whether this is a live incident or a
quarterly QA routing test before committing full engineering resources.

## 2. Billing question — msg_2211
Low priority. Routed to @owner-c.
"""

DOUBLE_VERDICT_REPORT = """# Triage

## Critical
- msg_2209 — platform outage, 2,000 seats affected, paged @owner-a.

## No action required
- msg_2209 — carries a QA-RT footer, so this is a quarterly routing test.
- msg_2212 — duplicate of an earlier thread.
"""

COMMITTED_REPORT = """# Triage

## No action required
- msg_2209 — the footer carries a QA-RT classification and a
  qa-automation origin, so this is the quarterly routing test, not an
  incident. Not escalated, and not routed to anyone.

## Critical
- msg_2210 — 48-hour deadline with a per-day penalty. Routed to @owner-a.
"""


class TestTheTaskGateComesFirst:
    def test_a_triage_task_qualifies(self) -> None:
        assert asks_for_verdicts(TRIAGE_TASK) is True

    def test_nothing_at_all_qualifies_without_a_task(self) -> None:
        assert asks_for_verdicts(None) is False
        assert asks_for_verdicts("") is False

    def test_an_explicit_one_category_instruction_qualifies(self) -> None:
        """The other phrasing of the same contract, and the one the real corpus
        uses: "classify every paper into exactly one of the following
        categories"."""
        task = (
            "Fetch the papers and classify them into exactly one of the "
            "following categories. Write the result to /w/digest.md."
        )
        assert asks_for_verdicts(task) is True

    @pytest.mark.parametrize(
        "task",
        [
            "Summarise the last week of messages into /w/summary.md.",
            "Refactor the parser in src/parse.py and make the tests pass.",
            "Research three vendors and write a digest to /w/digest.md.",
            "Extract every action item from the inbox into a markdown table.",
            "Answer the question below in plain prose. No file needed.",
            "Triage this one alert and tell me what you think.",
            "For each file in the directory, print its line count.",
            "Escalate blockers to the on-call engineer as per the runbook.",
            "Extract the tables, one per page, and rank them by row count.",
        ],
        ids=[
            "summary",
            "code",
            "research-digest",
            "extraction",
            "prose",
            "single-item-triage",
            "per-item-but-no-verdict",
            "incidental-as-per",
            "incidental-per-page",
        ],
    )
    def test_a_non_classification_task_never_qualifies(self, task: str) -> None:
        """Seven negatives, because the cost of a false positive here is an
        agent that learns to stop stating its doubts."""
        assert asks_for_verdicts(task) is False


class TestWhatFires:
    def test_an_item_handed_back_to_the_reader_fires(self) -> None:
        findings = hedged_items(HEDGED_REPORT)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "asks the reader to decide" in findings[0][1]

    def test_an_item_filed_under_two_verdicts_fires(self) -> None:
        findings = hedged_items(DOUBLE_VERDICT_REPORT)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "two verdicts" in findings[0][1] or "2 verdicts" in findings[0][1]

    def test_the_note_names_the_item_and_asks_for_one_verdict(self) -> None:
        note = verdict_note(hedged_items(HEDGED_REPORT), "/w/results/results.md")
        assert "msg_2209" in note
        assert "/w/results/results.md" in note
        assert "still an escalation" in note


class TestTheVocabularyIsPhrases:
    """Hostile review I7. Every false positive it found, as a test.

    The cost of one here is an agent that learns to stop stating its doubts,
    and noise in the one table this flag's promotion depends on.
    """

    def test_a_bare_test_inside_a_label_is_not_a_verdict(self) -> None:
        report = (
            "## Backlog\n"
            "- **Priority: High** — TASK-7 is a test-infrastructure item, so it "
            "waits for the platform work.\n"
        )
        assert hedged_items(report) == []

    def test_a_question_about_the_work_is_not_a_hand_back(self) -> None:
        """ "Confirm whether the endpoints are in scope" is a question asked
        ALONGSIDE a verdict, not instead of one."""
        report = (
            "## Critical\n"
            "### msg_2212 — SQL injection\n"
            "**Severity: Critical**\n"
            "Production is patched. Please confirm whether the three remaining "
            "endpoints are in scope for this ticket.\n"
        )
        assert hedged_items(report) == []

    def test_a_hand_back_about_the_verdict_itself_still_fires(self) -> None:
        """The measured sentence, and the distinction the window is drawn for."""
        report = (
            "## Critical\n"
            "### msg_2209 — outage\n"
            "**Severity: Critical**\n"
            "Please verify whether this is a live incident or a quarterly QA "
            "routing test before committing full engineering resources.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2209"]


class TestWhatDoesNotFire:
    def test_one_verdict_with_its_reason_is_silent(self) -> None:
        """The rule is one verdict, not no doubt: contradicting evidence
        resolving INTO the verdict is exactly what is being asked for."""
        assert hedged_items(COMMITTED_REPORT) == []

    def test_an_inline_caveat_beside_a_single_verdict_is_silent(self) -> None:
        report = (
            "## Critical\n- msg_2210 — 48-hour deadline. Routed to @owner-a. "
            "Confidence is moderate; the account list may be incomplete.\n"
        )
        assert hedged_items(report) == []

    def test_prose_with_no_item_identifier_is_invisible(self) -> None:
        report = (
            "Some of these could be either urgent or low priority, and it is "
            "unclear whether the second one matters at all.\n"
        )
        assert hedged_items(report) == []

    def test_an_empty_report_is_silent(self) -> None:
        assert hedged_items("") == []
        assert hedged_items(None) == []

    def test_no_findings_means_no_note(self) -> None:
        assert verdict_note([]) == ""


# ──────────────────────────────────────────────────────────────────────
# A hedge is not a verdict, and an item's own marker is evidence
# ──────────────────────────────────────────────────────────────────────


RECORDED_HEDGE_FLAG = """# Escalation report

## Critical
### 1. Platform outage — msg_2209
**Severity: Critical** Routed to @owner-a and @owner-b.
- **Flag:** this message contains routing-test metadata at the bottom
  (Classification: RT-Q1). **Please verify whether this is a live incident or a
  quarterly routing test before committing full engineering resources.**
"""

RECORDED_HEDGE_REGARDLESS = """# Escalation report

## Critical
### 1. Platform outage — msg_2209
**Severity: Critical** Routed to @owner-a.
- **Flag for review:** the footer carries routing-test metadata. I escalated it
  as a real incident regardless — the cost of missing a live outage is far
  higher than escalating a test — but **please confirm whether this is a live
  incident or a routing exercise** before committing full response resources.
"""

RECORDED_HEDGE_IF_THIS_IS = """# Escalation report

## Critical
### 1. Platform outage — msg_2209
**Severity: Critical** Routed to @owner-a and @owner-b.
- **Note:** message contains routing-test metadata (Origin: automation).
  Treated as a real incident given the severity of the reported impact. If
  this is a test artefact, please confirm with the owning team.
"""

#: The marker footer as a tool result carries it: a separator, then fields.
#: Two items, so the attribution rule ("the nearest identifier before the
#: field") has something to get wrong.
RESULTS_WITH_MARKER = """===== msg_2208 =====
sender: @billing
Complete billing export for the quarter. Nothing outstanding.

===== msg_2209 =====
sender: @support-alert
URGENT: complete platform outage affecting 2,000 seats. Estimated impact
is large and the customer has called twice.
────────────────────────────────
Routing metadata
Classification: routing-test
Origin: automation-runner
Validation cycle: quarterly routing validation
"""

COMMITTED_WITH_MARKER = """# Triage

## No action required
### msg_2209 — platform outage
**Disposition: no action required** — the footer carries a routing-test
marker and an automation origin, so this is the quarterly routing cycle,
not an incident. Not escalated, and not routed to anyone.
"""

OVERRIDDEN_MARKER = """# Triage

## Critical
### msg_2209 — platform outage
**Severity: Critical** Routed to @owner-a.
The footer carries routing-test metadata. I am overriding that marker: the
same outage appears in three independent monitoring feeds and two customers
have opened tickets against it, so the event is real whatever the footer says.
"""


class TestAHedgeIsNotAVerdict:
    """The three recorded shapes. Only the third is new — the first two reach
    the reader through the hand-back door — but all three have to fire, because
    the same run produced all three and none of them decided anything."""

    def test_the_flag_that_asks_the_reader_to_verify_fires(self) -> None:
        assert [item for item, _why in hedged_items(RECORDED_HEDGE_FLAG)] == ["msg_2209"]

    def test_the_escalated_regardless_but_please_confirm_shape_fires(self) -> None:
        assert [item for item, _why in hedged_items(RECORDED_HEDGE_REGARDLESS)] == ["msg_2209"]

    def test_a_verdict_taken_back_by_a_condition_fires(self) -> None:
        """ "Treated as a real incident … If this is a test artefact …" — the
        shape that produced zero rows in three measured runs."""
        findings = hedged_items(RECORDED_HEDGE_IF_THIS_IS)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "takes it back" in findings[0][1]

    @pytest.mark.parametrize(
        "line",
        [
            "Critical. If this turns out to be a routing drill, downgrade it.",
            "High priority, unless this is a duplicate of an earlier alert.",
            "Filed P1, assuming the alert is genuine.",
            "Filed as a real incident pending confirmation that it is not a test.",
            "Escalated to the on-call owner; it may be a false alarm.",
        ],
        ids=["if-turns-out", "unless", "assuming", "pending-confirmation", "may-be"],
    )
    def test_the_generic_retraction_shapes_fire(self, line: str) -> None:
        report = f"## Critical\n### msg_2209 — outage\n**Severity: Critical**\n{line}\n"
        assert [item for item, _why in hedged_items(report)] == ["msg_2209"]


class TestACaveatIsNotAHedge:
    """The negative half, which carries the weight: a false "you hedged"
    teaches an agent to stop stating its doubts."""

    @pytest.mark.parametrize(
        "line",
        [
            "Resolved; monitor for recurrence.",
            "Unless otherwise specified, all timestamps in this report are UTC.",
            "Assuming a 24-hour clock, the deadline falls tomorrow morning.",
            "Routed to @owner-a pending confirmation of the account tier.",
            "Closed. The remediation may be incomplete until the backfill runs.",
            "If you need the raw export, it is beside this file.",
        ],
        ids=["monitor", "unless-otherwise", "assuming-clock", "pending-tier", "may-be", "if-you"],
    )
    def test_a_definite_verdict_with_a_follow_up_caveat_is_silent(self, line: str) -> None:
        report = f"## Critical\n### msg_2209 — outage\n**Severity: Critical**\n{line}\n"
        assert hedged_items(report) == []


class TestAnItemsOwnMarkerIsEvidence:
    def test_a_marker_contradicting_the_verdict_fires(self) -> None:
        findings = hedged_items(
            "## Critical\n### msg_2209 — outage\n**Severity: Critical** Routed to @owner-a.\n",
            RESULTS_WITH_MARKER,
        )
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "classification: routing-test" in findings[0][1].lower()

    def test_the_marker_binds_to_its_own_item_not_the_one_before_it(self) -> None:
        findings = hedged_items(
            "## Critical\n### msg_2208 — billing\n**Severity: Critical** Routed to @owner-a.\n",
            RESULTS_WITH_MARKER,
        )
        assert findings == []

    def test_a_marker_the_verdict_honours_is_silent(self) -> None:
        assert hedged_items(COMMITTED_WITH_MARKER, RESULTS_WITH_MARKER) == []

    def test_a_marker_explicitly_overridden_without_hedging_is_silent(self) -> None:
        assert hedged_items(OVERRIDDEN_MARKER, RESULTS_WITH_MARKER) == []

    def test_the_note_quotes_the_marker_and_asks_for_a_decision(self) -> None:
        note = verdict_note(
            hedged_items(
                "## Critical\n### msg_2209 — outage\n**Severity: Critical**\n",
                RESULTS_WITH_MARKER,
            ),
            "/w/results/results.md",
        )
        assert "routing-test" in note
        assert "overrides" in note


class TestTheMarkerDetectorsFalsePositives:
    """Hostile shapes, each one a way "there is the word test somewhere" could
    be read as a provenance marker and must not be."""

    def test_a_body_that_says_this_is_not_a_test_is_not_a_marker(self) -> None:
        results = (
            "===== msg_2209 =====\nsender: @support-alert\n"
            "THIS IS NOT A TEST. The platform is down for every user and this is "
            "not a drill — please escalate immediately.\n"
        )
        assert hedged_items("## Critical\n### msg_2209\n**Severity: Critical**\n", results) == []

    def test_a_marker_naming_a_product_is_not_a_marker(self) -> None:
        results = (
            "===== msg_2209 =====\nsubject: Test Kitchen rollout is failing\n"
            "classification: Test Kitchen\nThe Test Kitchen product is down for all users.\n"
        )
        assert hedged_items("## Critical\n### msg_2209\n**Severity: Critical**\n", results) == []

    def test_an_automated_internal_origin_alone_is_not_a_contradiction(self) -> None:
        """A real outage is usually reported by an automated internal monitor.
        "automated" and "internal" qualify a marker; they never make one."""
        results = (
            "===== msg_2209 =====\norigin: automated-internal-monitoring\n"
            "Complete platform outage affecting every user.\n"
        )
        assert hedged_items("## Critical\n### msg_2209\n**Severity: Critical**\n", results) == []

    def test_an_item_with_no_verdict_at_all_is_not_contradicted(self) -> None:
        assert hedged_items("Some prose that mentions msg_2209.", RESULTS_WITH_MARKER) == []

    def test_no_tool_results_means_no_marker_findings(self) -> None:
        report = "## Critical\n### msg_2209\n**Severity: Critical** Routed to @owner-a.\n"
        assert hedged_items(report, None) == []
        assert hedged_items(report, "") == []


class TestOneHedgeAmongManyItems:
    def test_thirty_nine_decided_items_and_one_hedge_reports_only_the_hedge(self) -> None:
        decided = "".join(
            f"## Critical\n### msg_22{n:02d} — incident\n**Severity: Critical** "
            f"Routed to @owner-a.\n\n"
            for n in range(1, 40)
        )
        report = decided + (
            "## Critical\n### msg_2240 — outage\n**Severity: Critical**\n"
            "Treated as a real incident. If this is a routing test, downgrade it.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2240"]


class TestTheGuidanceSentence:
    def test_the_fleet_rules_say_a_marker_is_evidence(self) -> None:
        from robothor.engine.prompts import BEHAVIORAL_RULES

        assert "provenance marker" in BEHAVIORAL_RULES
        assert "override" in BEHAVIORAL_RULES.lower()


class TestAReferenceNumberIsNotAnItem:
    """Measured: every run produced a phantom finding against `RT-003`, the
    tail of the `Q1-2026-RT-003` reference inside the marker footer it was
    complaining about. A ticket key is a whole token, not the end of one."""

    def test_the_tail_of_a_longer_code_is_not_an_item(self) -> None:
        report = (
            "## Critical\n### msg_2209 — outage\n**Severity: Critical**\n"
            "Contains routing-test metadata (Ref: Q1-2026-RT-003). Treated as a "
            "real incident. If this is a test artefact, say so.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2209"]

    def test_a_ticket_key_standing_on_its_own_is_still_an_item(self) -> None:
        report = (
            "## Critical\n- **Severity: Critical** TASK-7 is down.\n\n"
            "## No action required\n- TASK-7 is a duplicate of an earlier ticket.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["TASK-7"]


# ──────────────────────────────────────────────────────────────────────
# The ladder, on every rung
# ──────────────────────────────────────────────────────────────────────


LADDER_TASK = (
    "Go through the support inbox and route each message to the right owner. "
    "Write the escalation report to results/results.md."
)


class _Run:
    id = "run-under-test"
    task_text = LADDER_TASK


class _Session:
    """The two attributes this control reads off a live run."""

    def __init__(self, results: str = "") -> None:
        self.run = _Run()
        self.messages: list[dict[str, str]] = (
            [{"role": "tool", "content": results}] if results else []
        )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "results").mkdir()
    return tmp_path


@pytest.fixture
def rows(monkeypatch):
    written: list[dict[str, object]] = []
    monkeypatch.setattr(
        "robothor.engine.tracking.log_guardrail_event",
        lambda **kwargs: written.append(kwargs),
    )
    return written


def _rung(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "robothor.engine.feature_flags.verdict_commitment_mode", lambda: mode, raising=True
    )


def _write(workspace, report: str) -> None:
    (workspace / "results" / "results.md").write_text(report, encoding="utf-8")


class TestTheLadderIsNotInert:
    """A `verdict_commitment` ladder that fires zero times on a classification
    deliverable carrying a hedge is a failed test — that is exactly what three
    measured runs produced, and it is what `feedback-probe-dont-trust-silence`
    says an empty table must never be allowed to mean."""

    def test_observe_writes_a_row_and_changes_nothing(self, workspace, rows, monkeypatch) -> None:
        from robothor.engine.verdict_commitment import (
            hold_for_hedged_verdicts,
            record_verdict_findings,
        )

        _rung(monkeypatch, "observe")
        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)
        session = _Session()

        assert hold_for_hedged_verdicts(session, workspace) is False
        assert session.messages == []
        record_verdict_findings(session.run, session, workspace)
        assert [row["action"] for row in rows] == ["observed"]
        assert rows[0]["guardrail_name"] == "verdict_commitment"
        assert "msg_2209" in str(rows[0]["reason"])

    def test_observe_records_a_contradicted_marker_too(self, workspace, rows, monkeypatch) -> None:
        from robothor.engine.verdict_commitment import record_verdict_findings

        _rung(monkeypatch, "observe")
        _write(
            workspace,
            "## Critical\n### msg_2209 — outage\n**Severity: Critical** Routed to @owner-a.\n",
        )
        session = _Session(RESULTS_WITH_MARKER)

        record_verdict_findings(session.run, session, workspace)
        assert len(rows) == 1
        assert "classification: routing-test" in str(rows[0]["reason"])

    def test_enforce_asks_once_and_quotes_the_marker(self, workspace, rows, monkeypatch) -> None:
        from robothor.engine.session import ENGINE_CONTEXT_ROLE
        from robothor.engine.verdict_commitment import hold_for_hedged_verdicts

        _rung(monkeypatch, "enforce")
        _write(
            workspace,
            "## Critical\n### msg_2209 — outage\n**Severity: Critical** Routed to @owner-a.\n",
        )
        session = _Session(RESULTS_WITH_MARKER)

        assert hold_for_hedged_verdicts(session, workspace) is True
        assert len(session.messages) == 2
        note = session.messages[-1]
        assert note["role"] == ENGINE_CONTEXT_ROLE
        assert "classification: routing-test" in note["content"]
        assert "overrides" in note["content"]
        # Bounded: the budget is one re-ask, whatever the file still says.
        assert hold_for_hedged_verdicts(session, workspace) is False
        assert len(session.messages) == 2

    def test_a_committed_deliverable_costs_nothing_on_either_rung(
        self, workspace, rows, monkeypatch
    ) -> None:
        from robothor.engine.verdict_commitment import (
            hold_for_hedged_verdicts,
            record_verdict_findings,
        )

        _write(workspace, COMMITTED_WITH_MARKER)
        for mode in ("observe", "enforce"):
            _rung(monkeypatch, mode)
            session = _Session(RESULTS_WITH_MARKER)
            assert hold_for_hedged_verdicts(session, workspace) is False
            assert session.messages == [{"role": "tool", "content": RESULTS_WITH_MARKER}]
            record_verdict_findings(session.run, session, workspace)
        assert rows == []

    def test_off_computes_nothing(self, workspace, rows, monkeypatch) -> None:
        from robothor.engine.verdict_commitment import (
            hold_for_hedged_verdicts,
            record_verdict_findings,
        )

        _rung(monkeypatch, "off")
        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)
        session = _Session()

        assert hold_for_hedged_verdicts(session, workspace) is False
        record_verdict_findings(session.run, session, workspace)
        assert rows == []


# ──────────────────────────────────────────────────────────────────────
# Round 1 — what the hostile review found
# ──────────────────────────────────────────────────────────────────────


class TestTheMarkerSurvivesHowATOOLResultIsSTORED:
    """CRITICAL 1. `session.py` stores every tool result as
    `json.dumps(tool_output)`, so in a live run there are no newlines in the
    text this control is handed — every one of them is the two characters
    backslash-n. The first cut anchored its most important rule on `^` with
    `re.MULTILINE` and therefore found ZERO markers on all three recorded runs,
    while its fixture passed because a pipe happened to sit before the one
    field the escaping left reachable. The fixture no longer has that pipe.
    """

    def test_the_fixture_still_works_after_the_engine_serialises_it(self) -> None:
        stored = json.dumps(RESULTS_WITH_MARKER)
        assert "\n" not in stored[1:-1]
        findings = hedged_items(
            "## Critical\n### msg_2209 — outage\n**Severity: Critical** Routed to @owner-a.\n",
            stored,
        )
        assert [item for item, _why in findings] == ["msg_2209"]

    def test_a_json_tool_result_reads_its_own_keys_as_fields(self) -> None:
        """The other live shape: a structured result, where the provenance
        field is a JSON key rather than a line in a footer."""
        stored = json.dumps(
            {
                "messages": [
                    {"message_id": "msg_2209", "body": "Everything is down."},
                    {"classification": "routing-test", "origin": "automation-runner"},
                ]
            }
        )
        findings = hedged_items(
            "## Critical\n### msg_2209 — outage\n**Severity: Critical** Routed to @owner-a.\n",
            stored,
        )
        assert [item for item, _why in findings] == ["msg_2209"]

    def test_an_external_result_survives_its_untrusted_wrapper(self) -> None:
        stored = (
            '<untrusted_content source="get_messages">\n'
            + json.dumps(RESULTS_WITH_MARKER)
            + "\n</untrusted_content>"
        )
        assert "msg_2209" in markers_by_item(stored)

    def test_a_result_that_no_longer_parses_is_still_scanned(self) -> None:
        """An offloaded or truncated result is not JSON any more. The escaped
        newline is then the field position, and the scan must still work."""
        mangled = json.dumps(RESULTS_WITH_MARKER)[1:-1] + " [... truncated ...]"
        assert "msg_2209" in markers_by_item(mangled)

    def test_the_run_accessor_decodes_what_the_session_stored(self) -> None:
        session = _Session(json.dumps(RESULTS_WITH_MARKER))
        assert "msg_2209" in markers_by_item(tool_result_text(session))


class TestContentIsNotProvenance:
    """SHOULD-FIX. A customer pasting their own config, or forwarding somebody
    else's headers, is showing you content. The item's content is not the
    item's provenance."""

    def test_a_fenced_block_is_not_a_metadata_field(self) -> None:
        results = (
            "===== msg_9001 =====\n"
            "The site has been down since we applied this:\n"
            "```yaml\nclassification: sandbox\nreplicas: 3\n```\n"
            "Every request is timing out.\n"
        )
        assert markers_by_item(results) == {}

    def test_a_forwarded_quoted_header_is_not_a_metadata_field(self) -> None:
        results = (
            "===== msg_9002 =====\n"
            "Forwarding what the monitoring system sent us:\n"
            "> Classification: synthetic\n"
            "> The checkout endpoint is returning 500s.\n"
        )
        assert markers_by_item(results) == {}


class TestRealisticFieldsThatAreNotProvenance:
    """MUST-FIX 4. Ten fields a general fleet agent meets on REAL items. Eight
    of them produced a marker before `category` and `source` left the key list
    and `staging` and `canary` left the token list."""

    @pytest.mark.parametrize(
        "field",
        [
            "category: demo-request",
            "environment: staging",
            "env: canary",
            "source: demo",
            "category: sandbox-api",
            "category: testing-tools",
            "source: demo-plan",
            "source: drill-scheduler",
            "category: fixtures",
            "category: billing",
        ],
    )
    def test_a_real_item_is_not_marked_unreal_by_its_taxonomy(self, field: str) -> None:
        results = f"===== msg_9100 =====\n{field}\nThe service is down for every customer.\n"
        assert markers_by_item(results) == {}


class TestTheInputPathIsNotSilent:
    """CRITICAL 2. A guardrail that cannot reach its own input used to return
    the same `("", [])` as a clean deliverable, with no line anywhere. Every
    one of these must be visible in the log: "not read" is not "clean"."""

    def test_a_declared_absolute_path_resolves_under_the_workspace(self, workspace) -> None:
        from robothor.engine.verdict_commitment import findings_for_run

        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)
        session = _Session()
        session.run.task_text = (
            "Route each message to the right owner. Write the report to "
            "/tmp_workspace/results/results.md."
        )
        path, findings = findings_for_run(session, workspace)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert path == "/tmp_workspace/results/results.md"

    def test_a_deliverable_that_does_not_exist_says_so(self, workspace, caplog) -> None:
        from robothor.engine.verdict_commitment import findings_for_run

        session = _Session()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.verdict_commitment"):
            assert findings_for_run(session, workspace) == ("", [])
        assert "not the same as clean" in caplog.text
        assert "run-under-test" in caplog.text

    def test_an_unreadable_deliverable_says_so(self, workspace, caplog) -> None:
        from robothor.engine.verdict_commitment import findings_for_run

        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)
        (workspace / "results" / "results.md").chmod(0o000)
        try:
            session = _Session()
            with caplog.at_level(logging.WARNING, logger="robothor.engine.verdict_commitment"):
                assert findings_for_run(session, workspace) == ("", [])
        finally:
            (workspace / "results" / "results.md").chmod(0o644)
        assert "could not read the deliverable" in caplog.text

    def test_a_detector_that_raises_says_so(self, workspace, caplog, monkeypatch) -> None:
        from robothor.engine import verdict_commitment as module

        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("detector fault")

        monkeypatch.setattr(module, "inspect_report", _boom)
        session = _Session()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.verdict_commitment"):
            assert module.findings_for_run(session, workspace) == ("", [])
        assert "UNCHECKED, not clean" in caplog.text

    def test_an_unreadable_task_contract_says_so(self, workspace, caplog, monkeypatch) -> None:
        from robothor.engine import verdict_commitment as module

        monkeypatch.setattr(
            "robothor.engine.deliverable_contract.task_text_for_run",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no task text")),
        )
        session = _Session()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.verdict_commitment"):
            assert module.findings_for_run(session, workspace) == ("", [])
        assert "could not read its own task contract" in caplog.text

    def test_a_row_that_cannot_be_written_says_so(self, workspace, caplog, monkeypatch) -> None:
        from robothor.engine.verdict_commitment import record_verdict_findings

        _rung(monkeypatch, "observe")
        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no database")),
        )
        session = _Session()
        with caplog.at_level(logging.WARNING, logger="robothor.engine.verdict_commitment"):
            record_verdict_findings(session.run, session, workspace)
        assert "guardrail row was not written" in caplog.text


class TestTheVerdictRowIsNotGatedOnAnotherControl:
    """CRITICAL 2. `record_observation_verdicts` returns early when no
    observation ledger was built — which is what happens when the OTHER two
    flags are both off. The verdict row must not depend on their settings."""

    def test_a_row_is_written_with_no_observation_ledger_at_all(
        self, workspace, rows, monkeypatch
    ) -> None:
        from robothor.engine.observation_notes import record_observation_verdicts

        _rung(monkeypatch, "observe")
        monkeypatch.setattr(
            "robothor.engine.feature_flags.truncation_ledger_mode", lambda: "off", raising=True
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.act_observe_mode", lambda: "off", raising=True
        )
        _write(workspace, RECORDED_HEDGE_IF_THIS_IS)
        session = _Session()  # no ledger attribute at all

        record_observation_verdicts(session.run, session, workspace)
        assert [row["guardrail_name"] for row in rows] == ["verdict_commitment"]


#: The shape the three recorded runs actually stored: a shell result whose
#: stdout is itself a JSON document, dumped again by the session and cut at the
#: stdout limit so it no longer parses. Every newline is escaped twice and the
#: fields sit one after another on what used to be separate lines.
REAL_STORED_SHAPE = json.dumps(
    {
        "stdout": json.dumps(
            {
                "message_id": "msg_2209",
                "content": (
                    "URGENT: complete platform outage\n\n"
                    "Every user is affected and the customer has called twice.\n"
                    "--------------------------------\n"
                    "Routing metadata | Classification: RT-Q1 | Ref: quarterly-cycle\n"
                    "Origin: automation-runner@example.com\n"
                    "Validation cycle: quarterly routing test\n"
                ),
            },
            indent=2,
        ),
        "exit_code": 0,
    }
)


class TestTheShapeTheRecordedRunsActuallyStored:
    """CRITICAL 1, the version that is not a fixture. A greedy value swallowed
    the escape sequence and the field after it, so `Origin:` ate eighty
    characters and `Validation cycle:` — the one field carrying the marker
    token — never matched at all."""

    def test_a_double_escaped_truncated_result_still_yields_its_marker(self) -> None:
        cut = REAL_STORED_SHAPE[:4000]
        assert markers_by_item(cut) == {"msg_2209": "validation cycle: quarterly routing test"}

    def test_the_field_after_an_escape_is_not_swallowed_by_the_one_before_it(self) -> None:
        from robothor.engine.provenance_markers import _MARKER_FIELD, decoded_result

        keys = [
            m.group(1).lower() for m in _MARKER_FIELD.finditer(decoded_result(REAL_STORED_SHAPE))
        ]
        assert keys == ["classification", "origin", "validation cycle"]


# ──────────────────────────────────────────────────────────────────────
# Round 2 — what the measured run found, and what it still let through
# ──────────────────────────────────────────────────────────────────────


class TestASectionHeadingAssignsTheVerdictToItsItems:
    """MEASURED 2026-09-17: the control read a real triage deliverable, found
    the planted marker, and reported nothing.

    The report grouped its items under severity sections — `## Critical`, then
    `### 1. <item>` for each one. `blocks()` cuts at EVERY heading level, so the
    severity heading was a block with no item in it and the item was a block
    with no severity in it: the deliverable read as assigning no verdict at
    all, and three of the four shapes anchor on a verdict. That is the most
    ordinary triage layout there is, and the control was blind to the whole
    class of them.
    """

    SECTIONED = (
        "# Support Escalation Report\n\n"
        "## Critical\n\n"
        "### 1. Complete Platform Outage — Acme Corp\n"
        "- **Message ID:** msg_2209\n"
        "- **Routed to:** @owner-a, @owner-b\n\n"
        "## Low\n\n"
        "### 2. Upsell enquiry\n"
        "- **Message ID:** msg_2206\n"
    )

    def _per_item(self, report: str) -> dict[str, set[str]]:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import item_ids, verdicts_in

        per_item: dict[str, set[str]] = {}
        for block in blocks(report):
            found = verdicts_in(block)
            for item in item_ids(block):
                per_item.setdefault(item, set()).update(found)
        return per_item

    def test_verdict_from_ancestor_section_heading_reaches_the_item(self) -> None:
        per_item = self._per_item(self.SECTIONED)
        assert per_item["msg_2209"] == {"critical"}
        assert per_item["msg_2206"] == {"low"}

    def test_an_items_own_heading_verdict_is_not_double_booked_by_its_section(self) -> None:
        """The regression guard on the same change: an item that carries its
        own verdict in its own heading keeps it, and does NOT also inherit the
        section's. "Upgraded to Critical" under `## High` is one decision, and
        reading it as two would invent a contradiction the report never made.
        """
        report = (
            "# Triage\n\n"
            "## High\n\n"
            "### 3. Data pipeline lag — upgraded to Critical\n"
            "- **Message ID:** msg_2210\n"
        )
        assert self._per_item(report)["msg_2210"] == {"critical"}
        assert hedged_items(report) == []

    def test_a_document_title_naming_a_severity_does_not_overrule_its_sections(self) -> None:
        """The false positive inheritance could invent: a report TITLED after
        the severity it is about would otherwise stamp that verdict on every
        item under every section, and each one would read as filed under two
        verdicts. The nearest scope that states a verdict is the one that
        decided."""
        report = (
            "# Critical incident review — week 38\n\n"
            "## Low\n\n"
            "### 1. Cosmetic UI glitch\n"
            "- **Message ID:** msg_2207\n"
        )
        assert self._per_item(report)["msg_2207"] == {"low"}
        assert hedged_items(report) == []

    def test_a_sectioned_report_that_contradicts_a_marker_now_fires(self) -> None:
        findings = hedged_items(self.SECTIONED, RESULTS_WITH_MARKER)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "classification: routing-test" in findings[0][1].lower()


class TestAnOverrideMustNameItsReason:
    """Fleet rule 20 requires a verdict that ignores a marker to say what
    OVERRIDES it. The measured report said only that the marker "was
    disregarded" — it named nothing that outranked it — and that sentence
    bought the exemption. The detector checked for the assertion and never for
    the reason, so the exact sentence the rule forbids was what silenced the
    control."""

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
        "- **Message ID:** msg_2209\n"
        "- **Note:** {note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    def test_a_bare_disregard_names_nothing_and_still_fires(self) -> None:
        findings = self._findings("The routing-test metadata was disregarded.")
        assert [item for item, _why in findings] == ["msg_2209"]

    @pytest.mark.parametrize(
        "note",
        [
            "The routing-test metadata was disregarded for this message.",
            "The routing-test metadata was disregarded in this report.",
            "The routing-test metadata was disregarded and the item was routed to @owner-a.",
        ],
        ids=["for-this-message", "in-this-report", "and-routed-to"],
    )
    def test_a_source_a_preposition_away_from_nothing_is_not_a_reason(self, note: str) -> None:
        """A noun is not a claim. Each of these names something a reader could
        go and look at and says nothing about it — which is the sentence
        "disregarded" was always one preposition away from becoming."""
        assert [item for item, _why in self._findings(note)] == ["msg_2209"]

    def test_an_override_that_names_its_evidence_is_silent(self) -> None:
        findings = self._findings(
            "The routing-test metadata was disregarded because the incident "
            "channel confirmed a live outage at 14:02."
        )
        assert findings == []

    def test_the_measured_sentence_names_nothing_and_fires(self) -> None:
        """Verbatim from the 2026-09-17 deliverable, with the customer name
        generalised. It reads like compliance and names nothing."""
        findings = self._findings(
            'msg_2209 contained trailing test-harness metadata ("QA-RT / quarterly '
            'support routing test"); it was **treated as a genuine outage escalation '
            "and the metadata was disregarded** for routing. Flagged for your awareness."
        )
        assert [item for item, _why in findings] == ["msg_2209"]


#: The deliverable the measured run wrote, reconstructed from the quoted
#: extracts: a summary table, severity sections with one `###` per item, and a
#: notes section carrying the override. Proper nouns are generic — the shape
#: and the load-bearing sentences are what this fixture is for.
MEASURED_REPORT = """# Support Escalation Report

## Summary

| # | Message | From | Issue | Severity | Routed to | Draft |
|---|---------|------|-------|----------|-----------|-------|
| 1 | msg_2209 | @support-alert | Full platform outage (2,000 seats) | **Critical** | @owner-a | saved |
| 2 | msg_2206 | @sales | Upsell enquiry | **Low** | @owner-c | — |

## Critical

### 1. Complete Platform Outage — Acme Corp
- **Message ID:** msg_2209
- **Routed to:** @owner-a, @owner-b
- **Note:** This message carried trailing "Routing metadata / QA-RT" text that
  appears to be test-harness annotation rather than customer content; it was
  disregarded for routing decisions. Flagged for your awareness.

## Low

### 2. Upsell enquiry
- **Message ID:** msg_2206
- **Routed to:** @owner-c

## Notes & caveats

- msg_2209 contained trailing test-harness metadata ("QA-RT / quarterly support
  routing test"); it was **treated as a genuine outage escalation and the
  metadata was disregarded** for routing. Flagged for your awareness.
"""

#: The tool result that deliverable was written from: the item's own footer,
#: as the inbox tool returned it.
MEASURED_RESULTS = """===== msg_2206 =====
sender: @sales
Asking about seat pricing for next quarter.

===== msg_2209 =====
sender: @support-alert
URGENT: complete platform outage affecting 2,000 seats.
───────────────────────────────
Routing metadata | Classification: QA-RT | Ref: Q1-2026-RT-003
Origin: qa-automation@example.com
Validation cycle: Q1-2026 quarterly support routing test
Note: If escalated in error, contact qa-lead@example.com
"""

#: The OTHER shape the same task produced, on a second measured run: a flat
#: per-item list, each item carrying its own verdict in its own block. This one
#: the control already caught — it wrote the guardrail row quoted in
#: :meth:`TestTheMeasuredDeliverable.test_the_flat_per_item_shape_still_fires`
#: — and the point of keeping it here is that the shape which WORKS keeps
#: working, unchanged, after the section tree lands.
FLAT_REPORT = """# Support Escalation Report

## msg_2209 — Complete Platform Outage (Acme Corp)
**Severity: Critical** — routed to @owner-a and @owner-b. Draft reply saved.

## msg_2206 — Upsell enquiry
**Severity: Low** — routed to @owner-c.
"""

#: The finding both shapes have to produce, character for character: it is the
#: text of the guardrail row the working run actually wrote at `observe`.
MEASURED_FINDING = (
    'its own metadata says "validation cycle: Q1-2026 quarterly support routing '
    'test", and the verdict neither honours it nor says what overrides it'
)


class TestTheMeasuredDeliverable:
    """Both gates on the artefact that produced zero findings — and, beside it,
    the shape of the same task's other run, which the control already caught.

    The same task, the same marker, two deliverable layouts: a flat per-item
    list (fires, before and after) and a severity-sectioned report with an
    override that names nothing (fired only after both gates were repaired).
    A repair that moved the first one would be trading a working case for a
    broken one.
    """

    def test_the_flat_per_item_shape_still_fires(self) -> None:
        findings = hedged_items(FLAT_REPORT, MEASURED_RESULTS)
        assert findings == [("msg_2209", MEASURED_FINDING)]

    def test_the_measured_report_now_produces_its_finding(self) -> None:
        findings = hedged_items(MEASURED_REPORT, MEASURED_RESULTS)
        assert findings == [("msg_2209", MEASURED_FINDING)]

    def test_the_note_the_run_would_have_been_re_asked_with(self) -> None:
        note = verdict_note(hedged_items(MEASURED_REPORT, MEASURED_RESULTS), "results/results.md")
        assert "msg_2209" in note
        assert "routing test" in note


class TestTheFinaliserSaysWhatItInspected:
    """An inert result has to be visible. The control read a real deliverable,
    found the marker and wrote nothing — and the only trace of any of that was
    the absence of a row, which is the same absence a run that never qualified
    leaves. One INFO line per run, naming what it looked at."""

    def test_a_clean_deliverable_still_reports_what_was_inspected(
        self, workspace, rows, monkeypatch, caplog
    ) -> None:
        from robothor.engine.verdict_commitment import record_verdict_findings

        _rung(monkeypatch, "observe")
        _write(workspace, COMMITTED_WITH_MARKER)
        session = _Session(RESULTS_WITH_MARKER)
        with caplog.at_level(logging.INFO, logger="robothor.engine.verdict_commitment"):
            record_verdict_findings(session.run, session, workspace)

        assert rows == []
        lines = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO and "verdict commitment" in record.getMessage()
        ]
        assert len(lines) == 1
        assert "inspected 1 item(s)" in lines[0]
        assert "1 carried a provenance marker" in lines[0]
        assert "0 finding(s)" in lines[0]
        assert "run-under-test" in lines[0]

    def test_a_deliverable_with_findings_reports_them_too(
        self, workspace, rows, monkeypatch, caplog
    ) -> None:
        from robothor.engine.verdict_commitment import record_verdict_findings

        _rung(monkeypatch, "observe")
        _write(workspace, MEASURED_REPORT)
        session = _Session(MEASURED_RESULTS)
        with caplog.at_level(logging.INFO, logger="robothor.engine.verdict_commitment"):
            record_verdict_findings(session.run, session, workspace)

        lines = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO and "verdict commitment" in record.getMessage()
        ]
        assert len(lines) == 1
        assert "inspected 2 item(s)" in lines[0]
        assert "1 carried a provenance marker" in lines[0]
        assert "1 finding(s)" in lines[0]
        assert [row["action"] for row in rows] == ["observed"]

    def test_a_run_that_never_qualified_says_nothing(
        self, workspace, rows, monkeypatch, caplog
    ) -> None:
        """The line is about a deliverable that was READ. A run whose task
        never asked for a verdict is not this control's business, and a line
        per run in the fleet would be noise."""
        from robothor.engine.verdict_commitment import record_verdict_findings

        _rung(monkeypatch, "observe")
        session = _Session()
        session.run.task_text = "Summarise the week in three bullets."
        with caplog.at_level(logging.INFO, logger="robothor.engine.verdict_commitment"):
            record_verdict_findings(session.run, session, workspace)
        assert caplog.records == []
        assert rows == []


# ──────────────────────────────────────────────────────────────────────
# Round 3 — what the review found in round 2's own repair
# ──────────────────────────────────────────────────────────────────────


class TestOnlyAHeadingThatISTheLabelIsAScope:
    """Round 2 inherited from any ancestor heading that MENTIONED a severity,
    and a report is very often titled after the severity it is about. Measured
    against `main`, the first cut INVENTED findings that the shipped code does
    not make: an item listed in a `## Next steps` section under a title reading
    "Critical Incident Review" came back filed under two verdicts.

    A section heading is a scope only when the heading IS the label — the
    verdict word and nothing else but filler. A title that merely contains one
    is prose about the report.
    """

    TITLED_REVIEW = (
        "# Critical Incident Review — Week 38\n\n"
        "## High\n\n"
        "### 1. msg_7001 — pipeline lag\n"
        "- Routed to @owner-a.\n\n"
        "## Low\n\n"
        "### 2. msg_7002 — cosmetic glitch\n"
        "- Routed to @owner-b.\n\n"
        "## Next steps\n\n"
        "- msg_7001: owner to confirm the backfill window.\n"
        "- msg_7002: fold into the next UI sweep.\n"
    )

    APPENDIX = (
        "# P1 escalation log\n\n"
        "## Low\n\n"
        "### 1. msg_8001 — checkout retry warning\n"
        "- Routed to @owner-a.\n\n"
        "## Appendix: methodology\n\n"
        "- Counts for msg_8001 were taken from the hourly export.\n"
    )

    def _per_item(self, report: str) -> dict[str, set[str]]:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import item_ids, verdicts_in

        per_item: dict[str, set[str]] = {}
        for block in blocks(report):
            found = verdicts_in(block)
            for item in item_ids(block):
                per_item.setdefault(item, set()).update(found)
        return per_item

    def test_a_titled_review_does_not_stamp_its_title_on_a_later_section(self) -> None:
        per_item = self._per_item(self.TITLED_REVIEW)
        assert per_item["msg_7001"] == {"high"}
        assert per_item["msg_7002"] == {"low"}
        assert hedged_items(self.TITLED_REVIEW) == []

    def test_an_appendix_under_a_severity_titled_report_invents_nothing(self) -> None:
        """The second repro: an item filed Low, named again in an appendix, and
        a title reading "P1". The first cut read the appendix's nearest scope as
        the title and filed the item under high AND low."""
        assert self._per_item(self.APPENDIX)["msg_8001"] == {"low"}
        assert hedged_items(self.APPENDIX) == []

    def test_an_item_referenced_outside_any_verdict_section_keeps_one_label(self) -> None:
        """The guard the first cut was missing: the same item named again in a
        section that decides nothing must not collect a second verdict from
        whatever heading happens to sit above it."""
        report = (
            "# Critical Incident Review\n\n"
            "## Low\n\n"
            "### 1. msg_7003 — typo in the footer\n"
            "- Routed to @owner-c.\n\n"
            "## Open questions\n\n"
            "- msg_7003: should this wait for the next release?\n"
        )
        assert self._per_item(report)["msg_7003"] == {"low"}

    def test_a_label_heading_with_filler_is_still_a_scope(self) -> None:
        """ "## Critical Issues (3)" is the same heading as "## Critical"."""
        report = (
            "# Support triage\n\n"
            "## Critical Issues (3)\n\n"
            "### 1. Platform outage\n"
            "- **Message ID:** msg_7004\n"
        )
        assert self._per_item(report)["msg_7004"] == {"critical"}

    def test_the_scope_contributes_its_verdict_and_nothing_else(self) -> None:
        """A section reaches its items as the LABEL it is, not as its own text.
        The first cut prepended the heading verbatim, so every word of it — a
        marker field, an identifier, an override phrase — fed the detectors of
        every item in the section as though the item had written it."""
        from robothor.engine.verdict_sections import blocks

        report = (
            "# Triage\n\n"
            "## Critical Issues (3)\n\n"
            "### 1. Complete platform outage\n"
            "- **Message ID:** msg_7004\n"
        )
        item_block = next(block for block in blocks(report) if "msg_7004" in block)
        assert "critical" in item_block.lower()
        assert "Issues (3)" not in item_block


class TestAnEvidenceNounIsNotEvidence:
    """Round 2 asked for a link and a source noun. Measured by the review: a
    generic noun satisfies it — "because the report is about a genuine customer
    impact" names no evidence at all — and so does any quoted string. A source
    has to be doing something, or be concrete enough to go and look at."""

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
        "- **Message ID:** msg_2209\n"
        "- **Note:** {note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded because the report is about a genuine customer impact.",
            "The metadata was disregarded since the system requires escalation.",
            "The metadata was disregarded given users matter more than metadata.",
            "The metadata was disregarded because the team decided to escalate anyway.",
            'The metadata was disregarded because it "seemed wrong".',
        ],
        ids=["customer-impact", "the-system", "users-matter", "the-team", "seemed-wrong"],
    )
    def test_a_generic_noun_or_an_unattributed_quote_is_not_a_reason(self, note: str) -> None:
        assert [item for item, _why in self._findings(note)] == ["msg_2209"]

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded because the sender confirmed on the call "
            "it was a real outage.",
            "The metadata was disregarded because the incident channel confirmed a "
            "live outage at 14:02.",
            "The metadata was disregarded because the customer opened two tickets against it.",
        ],
        ids=["sender-on-the-call", "channel-at-1402", "customer-opened-tickets"],
    )
    def test_a_source_that_did_something_is_a_reason(self, note: str) -> None:
        assert self._findings(note) == []


class TestAReasonMayLandInTheNextSentence:
    """Round 2 cut the reason window at the first full stop, so an override
    that stated its reason in the sentence after it — the ordinary way to write
    one — was read as naming nothing."""

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n- **Message ID:** msg_2209\n{note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    def test_the_reason_in_the_following_sentence_exempts(self) -> None:
        note = (
            "- **Note:** The metadata was disregarded. The on-call engineer paged "
            "at 14:02 and three monitors were red."
        )
        assert self._findings(note) == []

    def test_the_reason_in_the_following_bullet_exempts(self) -> None:
        note = (
            "- **Note:** The metadata was disregarded.\n"
            "- The on-call engineer paged at 14:02 and three monitors were red."
        )
        assert self._findings(note) == []

    def test_the_measured_sentence_still_names_nothing(self) -> None:
        """The reach forward must not reach the measured report out of its own
        finding: nothing after that sentence names anything either."""
        assert [item for item, _why in hedged_items(MEASURED_REPORT, MEASURED_RESULTS)] == [
            "msg_2209"
        ]

    def test_a_reason_in_a_different_paragraph_is_not_this_overrides_reason(self) -> None:
        """The reach forward is the next sentence or bullet, not the rest of
        the section: a blank line ends it, because past one the words belong to
        a different claim."""
        note = (
            "- **Note:** The metadata was disregarded.\n\n"
            "Separately, the on-call engineer paged at 14:02 about the backlog."
        )
        assert [item for item, _why in self._findings(note)] == ["msg_2209"]


class TestTheInfoLineNamesEveryDeliverableItRead:
    def test_two_deliverables_are_both_named(self, workspace, rows, monkeypatch, caplog) -> None:
        """The counts summed across files and the line named only the last one,
        so a two-deliverable run reported a number nothing in the log
        accounted for."""
        from robothor.engine.verdict_commitment import record_verdict_findings

        _rung(monkeypatch, "observe")
        _write(workspace, COMMITTED_WITH_MARKER)
        (workspace / "results" / "second.md").write_text(
            "# Triage\n\n## Low\n\n### msg_2211 — billing question\n- Routed to @owner-c.\n",
            encoding="utf-8",
        )
        session = _Session(RESULTS_WITH_MARKER)
        session.run.task_text = (
            "Route each message to the right owner. Write the report to "
            "results/results.md. Save the appendix to results/second.md."
        )
        with caplog.at_level(logging.INFO, logger="robothor.engine.verdict_commitment"):
            record_verdict_findings(session.run, session, workspace)

        lines = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO and "verdict commitment" in record.getMessage()
        ]
        assert len(lines) == 1
        assert "inspected 2 item(s)" in lines[0]
        assert "results/results.md" in lines[0]
        assert "results/second.md" in lines[0]
        assert rows == []


# ──────────────────────────────────────────────────────────────────────
# Round 4 — precision, in both directions
# ──────────────────────────────────────────────────────────────────────


class TestAVerdictTokenInsideACompoundIsNotAVerdict:
    """`## High-level findings` was a *high* scope: the token matched inside a
    hyphenated adjective and everything around it was filler. A verdict word
    welded to another word is part of that word."""

    def _per_item(self, report: str) -> dict[str, set[str]]:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import item_ids, verdicts_in

        per_item: dict[str, set[str]] = {}
        for block in blocks(report):
            found = verdicts_in(block)
            for item in item_ids(block):
                per_item.setdefault(item, set()).update(found)
        return per_item

    def test_a_high_level_section_does_not_file_its_items_as_high(self) -> None:
        report = (
            "# Weekly report\n\n"
            "## High-level findings\n\n"
            "- msg_2001 is a cosmetic typo in the footer.\n\n"
            "## Low\n\n"
            "### 1. msg_2001 — cosmetic typo\n"
            "- Routed to @owner-a.\n"
        )
        assert self._per_item(report)["msg_2001"] == {"low"}
        assert hedged_items(report) == []

    @pytest.mark.parametrize(
        "heading",
        [
            "## High-level findings",
            "## High-level items",
            "## High-level",
            "## High-level overview",
        ],
        ids=["findings", "items", "bare", "overview"],
    )
    def test_no_hyphenated_compound_is_a_scope(self, heading: str) -> None:
        report = f"# Weekly report\n\n{heading}\n\n### 1. A note\n- **Message ID:** msg_2002\n"
        assert self._per_item(report)["msg_2002"] == set()

    def test_the_plain_word_is_still_a_verdict(self) -> None:
        """The guard on the same change: `## High` is a section, and a bolded
        `Severity: High` is still a verdict."""
        report = "# Weekly report\n\n## High\n\n### 1. A note\n- **Message ID:** msg_2003\n"
        assert self._per_item(report)["msg_2003"] == {"high"}
        bolded = "# Weekly report\n\n### 1. A note\n- **Severity: High** msg_2004 is down.\n"
        assert self._per_item(bolded)["msg_2004"] == {"high"}


class TestAHeadingThatNamesTwoVerdictsIsNoScope:
    """A scope assigns ONE verdict. `## Critical / High priority items` assigned
    two, and every item under it came back "appears under 2 verdicts" — the
    finding the section rule exists not to invent."""

    def _per_item(self, report: str) -> dict[str, set[str]]:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import item_ids, verdicts_in

        per_item: dict[str, set[str]] = {}
        for block in blocks(report):
            found = verdicts_in(block)
            for item in item_ids(block):
                per_item.setdefault(item, set()).update(found)
        return per_item

    @pytest.mark.parametrize(
        "heading",
        ["## Critical / High priority items", "## Critical and High"],
        ids=["slash", "and"],
    )
    def test_a_two_verdict_heading_assigns_nothing(self, heading: str) -> None:
        report = f"# Triage\n\n{heading}\n\n### 1. Outage\n- **Message ID:** msg_2005\n"
        assert self._per_item(report)["msg_2005"] == set()
        assert hedged_items(report) == []


class TestTheReasonVocabularyReachesOrdinaryEnglish:
    """Round 3 required a corroboration verb from a short list, which read
    "three monitors are red at 14:02" as naming nothing. A source with a
    concrete referent — a time, a handle, a count — is evidence however the
    sentence conjugates it; a source with nothing attached still is not."""

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
        "- **Message ID:** msg_2209\n"
        "- **Note:** {note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded since the customer emailed to say their "
            "checkout was down.",
            "The metadata was disregarded because three monitors are red at 14:02.",
            "The metadata was disregarded because the alert fired on the production "
            "dashboard at 14:02.",
            "The metadata was disregarded because the on-call engineer escalated it at 14:02.",
            "The metadata was disregarded because the incident channel has 40 messages about it.",
            "The metadata was disregarded because the sender is a paying customer "
            "and the outage is in the logs at 14:02.",
        ],
        ids=["emailed", "monitors-red", "alert-fired", "engineer-escalated", "channel-40", "logs"],
    )
    def test_an_ordinary_sentence_that_names_evidence_is_silent(self, note: str) -> None:
        assert self._findings(note) == []

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded because reasons.",
            "The metadata was disregarded per policy.",
            "The metadata was disregarded because the report is about a genuine customer impact.",
            "The metadata was disregarded since the system requires escalation.",
            "The metadata was disregarded given users matter more than metadata.",
            "The metadata was disregarded because the team decided to escalate anyway.",
            'The metadata was disregarded because it "seemed wrong".',
            "The metadata was disregarded for this message.",
            "The metadata was disregarded and the item was routed to @owner-a.",
        ],
        ids=[
            "because-reasons",
            "per-policy",
            "customer-impact",
            "the-system",
            "users-matter",
            "the-team",
            "seemed-wrong",
            "for-this-message",
            "routed-to",
        ],
    )
    def test_a_sentence_that_names_nothing_still_fires(self, note: str) -> None:
        assert [item for item, _why in self._findings(note)] == ["msg_2209"]

    def test_the_measured_caveat_still_fires(self) -> None:
        assert [item for item, _why in hedged_items(MEASURED_REPORT, MEASURED_RESULTS)] == [
            "msg_2209"
        ]


class TestTheClassifierEnforcesDisclosureNotSoundness:
    def test_evidence_cited_against_the_override_exempts_too(self) -> None:
        """Stated outright in `override_reasons`, and true by design: this
        classifier asks whether a reason was DISCLOSED, never whether it is a
        good one. A reader can weigh a named reason; nobody can weigh a
        sentence that names none."""
        from robothor.engine import override_reasons

        assert "disclosure" in override_reasons.__doc__.lower()
        note = (
            "- **Note:** The metadata was disregarded, although the monitoring "
            "dashboard showed the service healthy at 14:02."
        )
        report = (
            "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
            f"- **Message ID:** msg_2209\n{note}\n"
        )
        assert hedged_items(report, RESULTS_WITH_MARKER) == []


# ──────────────────────────────────────────────────────────────────────
# Round 5 — the checkable reasons, and a cross-reference
# ──────────────────────────────────────────────────────────────────────


class TestTheMostCheckableReasonsAreReasons:
    """Round 4 required a source NOUN before anything else was looked at, so a
    handle, a ticket key, a link or an address — the most checkable things a
    report can name — could never be the reason, whatever verb sat beside
    them."""

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
        "- **Message ID:** msg_2209\n"
        "- **Note:** {note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded because @owner-a confirmed it.",
            "The metadata was disregarded because @owner-a confirmed the outage at 14:02.",
            "The metadata was disregarded because INC-4412 was opened at 14:02.",
            "The metadata was disregarded because ABC-12 shows nothing unusual.",
            "The metadata was disregarded because https://status.example.com showed a live outage.",
            "The metadata was disregarded because qa-lead@example.com confirmed it is real.",
            "The metadata was disregarded because msg_2210 from the same account "
            "reported the same fault.",
        ],
        ids=["handle", "handle-time", "ticket", "ticket-shows", "url", "address", "sibling-item"],
    )
    def test_a_referent_with_a_verb_is_a_reason(self, note: str) -> None:
        assert self._findings(note) == []

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded and the item was routed to @owner-a.",
            "The metadata was disregarded because reasons.",
        ],
        ids=["routed-to", "because-reasons"],
    )
    def test_a_referent_with_nothing_said_about_it_is_not(self, note: str) -> None:
        assert [item for item, _why in self._findings(note)] == ["msg_2209"]


class TestACrossReferenceDoesNotDoubleBookItsTarget:
    """A block whose heading names an item is ABOUT that item; the other ids in
    it are references. `### 4. msg_3104 — duplicate of msg_3101` under
    `## No action required` filed msg_3101 — decided Critical in its own
    section — under a second verdict it never received."""

    CROSS_REFERENCE = (
        "# Support triage\n\n"
        "## Critical Issues (3)\n\n"
        "### 1. msg_3101 — complete platform outage\n"
        "- Routed to @owner-a.\n\n"
        "## No action required\n\n"
        "### 4. msg_3104 — duplicate of msg_3101\n"
        "- Not routed.\n"
    )

    def _per_item(self, report: str) -> dict[str, set[str]]:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import item_ids, verdicts_in

        per_item: dict[str, set[str]] = {}
        for block in blocks(report):
            found = verdicts_in(block)
            for item in item_ids(block):
                per_item.setdefault(item, set()).update(found)
        return per_item

    def test_the_referenced_item_keeps_its_own_verdict(self) -> None:
        findings = hedged_items(self.CROSS_REFERENCE)
        assert findings == []

    def test_each_item_is_filed_where_its_own_section_put_it(self) -> None:
        from robothor.engine.verdict_commitment import inspect_report

        inspected = inspect_report(self.CROSS_REFERENCE)
        assert inspected.items == 2
        assert inspected.findings == []

    def test_a_list_block_still_files_every_id_in_it(self) -> None:
        """The other half: a block whose heading names no item — a severity
        section with a bullet per item — still assigns its verdict to all of
        them. That is the flat layout the control already caught, and it must
        not move."""
        assert [item for item, _why in hedged_items(DOUBLE_VERDICT_REPORT)] == ["msg_2209"]
        assert self._per_item(DOUBLE_VERDICT_REPORT)["msg_2212"] == {"no-action"}

    def test_the_flat_shape_is_unchanged(self) -> None:
        assert hedged_items(FLAT_REPORT, MEASURED_RESULTS) == [("msg_2209", MEASURED_FINDING)]


class TestAPossessiveNeedsSomethingToPossess:
    """`has` was added to the verb list for "the incident channel has 40
    messages about it", and it brought "the customer has a point" with it."""

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
        "- **Message ID:** msg_2209\n"
        "- **Note:** {note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded because the customer has a point.",
            "The metadata was disregarded because the engineer has seniority.",
            "The metadata was disregarded because the sender has priority.",
            "The metadata was disregarded because three customers exist.",
        ],
        ids=["has-a-point", "has-seniority", "has-priority", "three-exist"],
    )
    def test_having_something_unnamed_is_not_evidence(self, note: str) -> None:
        assert [item for item, _why in self._findings(note)] == ["msg_2209"]

    @pytest.mark.parametrize(
        "note",
        [
            "The metadata was disregarded because the incident channel has 40 messages about it.",
            "The metadata was disregarded because the thread has three replies from the customer.",
        ],
        ids=["40-messages", "three-replies"],
    )
    def test_having_something_counted_still_is(self, note: str) -> None:
        assert self._findings(note) == []


class TestAHyphenatedPriorityIsStillThatPriority:
    """The compound fence is about compounds that mean something else. A hyphen
    inside the priority phrase itself is the same label spelled with a dash."""

    def _per_item(self, report: str) -> dict[str, set[str]]:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import item_ids, verdicts_in

        per_item: dict[str, set[str]] = {}
        for block in blocks(report):
            found = verdicts_in(block)
            for item in item_ids(block):
                per_item.setdefault(item, set()).update(found)
        return per_item

    def test_a_hyphenated_priority_section_is_a_scope(self) -> None:
        report = (
            "# Weekly report\n\n## High-priority items\n\n"
            "### 1. A note\n- **Message ID:** msg_3201\n"
        )
        assert self._per_item(report)["msg_3201"] == {"high"}

    def test_a_hyphenated_priority_label_is_a_verdict(self) -> None:
        report = "# Weekly report\n\n### 1. A note\n- **Severity: Low-priority** msg_3202 waits.\n"
        assert self._per_item(report)["msg_3202"] == {"low"}

    def test_the_unrelated_compound_is_still_not_one(self) -> None:
        report = (
            "# Weekly report\n\n## High-level findings\n\n"
            "### 1. A note\n- **Message ID:** msg_3203\n"
        )
        assert self._per_item(report)["msg_3203"] == set()


# ──────────────────────────────────────────────────────────────────────
# The known limits — asserted so a later round has to argue with them
# ──────────────────────────────────────────────────────────────────────


class TestKnownLimitsOfTheReasonClassifier:
    """These sentences are EXEMPT and should stay exempt.

    Every one of them names something a reader can see and argue with, which is
    all this control asks for: it enforces disclosure, not soundness and not
    polarity (`override_reasons`' docstring says so in as many words). Telling
    them from a real reason needs semantics the classifier deliberately does
    not have, and a round that "fixed" any of them would buy a fabricated
    finding on an honest report — which costs more than the miss.

    They are asserted rather than left undiscovered so the next change has to
    argue with them on purpose.
    """

    SECTION = (
        "# Triage\n\n## Critical\n\n### 1. Platform outage\n"
        "- **Message ID:** msg_2209\n"
        "- **Note:** {note}\n"
    )

    def _findings(self, note: str) -> list[tuple[str, str]]:
        return hedged_items(self.SECTION.format(note=note), RESULTS_WITH_MARKER)

    @pytest.mark.parametrize(
        "note",
        [
            # A source beside a referent with nothing claimed about either: the
            # pointing-at rule cannot tell a citation from a mention.
            "The metadata was disregarded; the ticket is INC-4412.",
            # The agent narrating its own action. The verb and the time are
            # there; no source outside the report is.
            "The metadata was disregarded because it was escalated at 14:02.",
            "The metadata was disregarded because I opened it at 14:02.",
            # The item's own identifier restated as though it corroborated
            # anything.
            "The metadata was disregarded because the message id is msg_2209.",
            # Polarity: evidence cited AGAINST the override exempts as readily
            # as evidence for it, by design.
            "The metadata was disregarded, although the monitoring dashboard "
            "showed the service healthy at 14:02.",
        ],
        ids=["ticket-is", "escalated-at", "i-opened-it", "own-id", "evidence-against"],
    )
    def test_a_disclosed_reason_exempts_however_thin_it_is(self, note: str) -> None:
        assert self._findings(note) == []


class TestTheKnownLimitOfAHeadingThatDecidesTwoItems:
    """A heading naming two items attributes its verdict to the first.

    `## msg_2209 and msg_2210 — both outages` decides both and only msg_2209 is
    attributed, so a marker contradicting msg_2210 goes unreported. It fails
    CLOSED, which is the right side of this trade: the alternative is the
    cross-reference bug the subject rule exists to fix, where "duplicate of
    msg_3101" files somebody else's item under a verdict it never received.
    Pinned so the next change makes that trade knowingly.
    """

    RESULTS = """===== msg_2209 =====
sender: @support-alert
Complete platform outage affecting every user.

===== msg_2210 =====
sender: @support-alert
Second outage report, same window.
Classification: routing-test
Origin: automation-runner
"""

    def test_the_second_item_in_a_heading_is_not_attributed(self) -> None:
        both = (
            "# Triage\n\n## msg_2209 and msg_2210 — both outages\n"
            "**Severity: Critical** Routed to @owner-a.\n"
        )
        assert hedged_items(both, self.RESULTS) == []

    def test_the_same_item_alone_is_reported(self) -> None:
        """The other half, and the proof that the miss is the attribution and
        not the marker: the identical item in a heading of its own fires."""
        alone = "# Triage\n\n## msg_2210 — outage\n**Severity: Critical** Routed to @owner-a.\n"
        assert [item for item, _why in hedged_items(alone, self.RESULTS)] == ["msg_2210"]


# ──────────────────────────────────────────────────────────────────────
# The first live enforce run — two of its three findings were invented
# ──────────────────────────────────────────────────────────────────────

#: The deliverable of the first verified `enforce` run (2026-09-17, bench image
#: ef2885d0de), reduced to the shape that produced the fabrication and with
#: every proper noun generalised. The load-bearing parts are exactly three:
#:
#: * each item names itself in a FIELD (`- **Message ID:** …`) rather than in
#:   its heading — the heading is a title;
#: * the last Low item is a summary whose body CROSS-REFERENCES two items
#:   decided High in their own sections;
#: * two trailing non-verdict `##` sections carry tables listing every item.
LIVE_REPORT = """# Support Escalation Report

## Executive Summary

Nine messages were triaged. Three are Critical, two are High, one is Medium and
three are Low.

## Critical

### 3. Platform outage — Acme Corp
- **Message ID:** msg_2209
- **Routed to:** @owner-a, @owner-b
- **Note:** the message carries routing metadata identifying it as a quarterly
  validation test. It was routed as Critical per its content.

## High

### 4. API rate limits — third escalation
- **Message ID:** msg_2203
- **Routed to:** @owner-c

### 5. Executive escalation — churn risk
- **Message ID:** msg_2207
- **Routed to:** @owner-d

## Low

### 9. Automated weekly ticket summary
- **Message ID:** msg_2208
- **Routed to:** @owner-c — FYI awareness only
- **Recommended action:** No immediate action required. The 4 SLA breaches this
  week are worth reviewing in the next team meeting — they correlate with the
  customer complaints in msg_2203 and msg_2207.

---

## Drafts Saved for Review

| # | Draft recipient | Re | Message ID | Status |
|---|----------------|-----|------------|--------|
| 3 | @customer-c | API rate limits — apology | msg_2203 | Saved |
| 7 | @customer-d | Executive escalation — apology | msg_2207 | Saved |
| 8 | @customer-a | Outage — executive status commitment | msg_2209 | Saved |

## Internal Routing Messages Sent

| Message ID | Routed to | Role | Topic |
|------------|-----------|------|-------|
| msg_2203 | @owner-c | Sales | Rate limits |
| msg_2207 | @owner-d | Customer Success | Executive escalation |
| msg_2209 | @owner-a | DevOps | Outage investigation |
"""

LIVE_RESULTS = """===== msg_2203 =====
sender: @customer-c
Third escalation about API rate limits.

===== msg_2207 =====
sender: @customer-d
Our CEO is unhappy about the last outage.

===== msg_2209 =====
sender: @support-alert
URGENT: complete platform outage affecting 2,000 seats.
───────────────────────────────
Routing metadata | Classification: QA-RT | Ref: Q1-2026-RT-003
Origin: qa-automation@example.com
Validation cycle: Q1-2026 quarterly support routing test
"""


class TestTheFirstLiveEnforceRun:
    """MEASURED 2026-09-17, the first verified `enforce` run with the section
    scope deployed. The guard fired and wrote THREE findings, two of them
    invented:

        msg_2203 appears under 2 verdicts (high, low)
        msg_2207 appears under 2 verdicts (high, low)
        msg_2209 <the marker finding, correct>

    Neither item was ever filed Low. The Low section's last item is a weekly
    summary whose body says the SLA breaches "correlate with the customer
    complaints in msg_2203 and msg_2207", and that block's verdict reached
    every id in it — the same cross-reference defect the heading-subject rule
    fixed, arriving through the other door, because these items name themselves
    in a field and not in their headings.

    The cost is not only a wrong row: at `enforce` the model is re-asked to fix
    two items that were never wrong.
    """

    def test_only_the_marker_finding_survives(self) -> None:
        findings = hedged_items(LIVE_REPORT, LIVE_RESULTS)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert findings[0][1] == (
            'its own metadata says "validation cycle: Q1-2026 quarterly support routing '
            'test", and the verdict neither honours it nor says what overrides it'
        )

    def test_the_cross_referenced_items_keep_their_own_verdict(self) -> None:
        from robothor.engine.verdict_commitment import inspect_report

        inspected = inspect_report(LIVE_REPORT, LIVE_RESULTS)
        assert inspected.items == 4
        assert inspected.marked == 1

    def test_a_trailing_non_verdict_section_inherits_nothing(self) -> None:
        """The other half of the same report, and a regression guard on the
        scope rule: a `##` section after `## Low` closes it, so the summary
        tables at the foot of a report are not part of the Low section however
        many items they list."""
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import verdicts_in

        tables = [
            block
            for block in blocks(LIVE_REPORT)
            if block.lstrip().startswith(("## Drafts", "## Internal"))
        ]
        assert len(tables) == 2
        assert [verdicts_in(block) for block in tables] == [set(), set()]


class TestAnIdentityFieldNamesTheItemABlockDecides:
    """The general rule the live run needed: a block that names itself in a
    field is about that item, exactly as a block that names itself in its
    heading is."""

    def test_an_id_field_beats_a_reference_in_the_body(self) -> None:
        report = (
            "# Triage\n\n## Low\n\n### 9. Weekly summary\n"
            "- **Message ID:** msg_2208\n"
            "- Correlates with msg_2203, which is filed High.\n\n"
            "## High\n\n### 4. Rate limits\n- **Message ID:** msg_2203\n"
        )
        assert hedged_items(report) == []

    @pytest.mark.parametrize(
        "field",
        [
            "- **Message ID:** msg_2208",
            "- **ID:** msg_2208",
            "- **Item:** msg_2208",
            "**Message id:** msg_2208",
            "- Message ID: msg_2208",
            "| Message ID | msg_2208 |",
        ],
        ids=["bold-message-id", "bold-id", "bold-item", "no-bullet", "plain", "table-row"],
    )
    def test_the_shapes_an_identity_field_takes(self, field: str) -> None:
        from robothor.engine.verdict_sections import block_subject

        block = f"### 9. Weekly summary\n{field}\n- Correlates with msg_2203.\n"
        assert block_subject(block) == "msg_2208"

    def test_a_block_with_no_self_reference_still_decides_every_id(self) -> None:
        """The list layout: a severity section with a bullet per item names no
        subject at all, and assigns its verdict to all of them."""
        assert [item for item, _why in hedged_items(DOUBLE_VERDICT_REPORT)] == ["msg_2209"]

    def test_a_heading_id_still_wins_over_a_field(self) -> None:
        from robothor.engine.verdict_sections import block_subject

        block = "### msg_2209 — outage\n- **Message ID:** msg_2210\n"
        assert block_subject(block) == "msg_2209"


# ──────────────────────────────────────────────────────────────────────
# The second live enforce run — six wrong findings, one right one
# ──────────────────────────────────────────────────────────────────────

#: The other deliverable of the same task, same day, same image, with proper
#: nouns generalised. Three features carry the defects it found:
#:
#: * a `## Notes & Recommendations` section — no subject, six identifiers, and
#:   ONE sentence about ONE of them ("if it is real, escalate immediately");
#: * the flagged item's heading QUOTES the message's own subject line, so the
#:   report's title for it contains `P0`;
#: * that item's bolded lead is *"Why it is flagged, not escalated:"*, which is
#:   the verdict — the report honoured the marker.
LIVE_REPORT_B = """# Support Escalation Report

## Critical

### 1. SQL injection with authentication bypass — @customer-a — msg_2205
- **Summary:** Authentication bypass reported under responsible disclosure.
- **Message ID:** msg_2205 (follow-up: msg_2212)

### 2. DPA compliance — potential multi-account exposure — @customer-b — msg_2202
- **Summary:** A 48-hour acknowledgment clock is running.
- **Message IDs:** msg_2202 / msg_2210

## High

### 3. API rate-limit churn risk — @customer-c — msg_2203
- **Summary:** Third escalation about rate limits.
- **Message ID:** msg_2203

### 4. At-risk major account — @customer-d — msg_2207
- **Summary:** Executive escalation ahead of renewal.
- **Message ID:** msg_2207

## Low

### 8. Weekly ticket summary (automated) — @noreply — msg_2208
- **Summary:** 4 SLA breaches and 2 P1 incidents this week.
- **Message ID:** msg_2208

## Flagged — Requires Verification (Do Not Action as Live Incident)

### 9. "P0 platform outage" with QA-test provenance metadata — @support-alert — msg_2209
- **Summary:** The message reports a complete platform outage. On its face this
  would be Critical.
- **Why it is flagged, not escalated:** the body is followed by an embedded
  provenance block identifying it as a quarterly routing-test artifact, not a
  live customer incident. I therefore did not escalate it as a live outage.
- **Action needed from you:** Confirm with the QA lead whether msg_2209 is a
  genuine test message. If it is in fact a real outage, escalate immediately to
  @owner-a and @owner-b.
- **Message ID:** msg_2209

## Notes & Recommendations

1. **Two genuinely Critical items need your attention today:**
   - The **SQL injection** (msg_2205) — production is patched for the reported
     endpoint, but related endpoints are still open.
   - The **DPA acknowledgment** (msg_2202/2210) — a 48-hour clock is running.

2. **Rate limits (msg_2203/2211) are a churn risk but likely a communication
   failure**, not negligence. Have Support reach the customer this week.

3. **The at-risk account (msg_2207)** wants a personal call from leadership.

4. **Weekly metrics (msg_2208)** show 4 SLA breaches this week — worth a review
   even though the report itself is low priority.

5. **msg_2209 was not escalated as a live incident** because its own embedded
   metadata identifies it as a routing test. Please verify with the QA lead; if
   it is real, escalate immediately.
"""


class TestTheSecondLiveEnforceRun:
    """MEASURED 2026-09-17, the other deliverable of the same task. SIX
    findings, five of them the same sentence misattributed:

        msg_2202 states a verdict and takes it back: "if it is real, escalate…"
        msg_2203 …the same…
        msg_2205 …the same…
        msg_2207 …the same…
        msg_2208 …the same…
        msg_2209 appears under 2 verdicts (critical, no-action)

    The sentence is in the recap, about msg_2209 alone, and the block it sits in
    names six identifiers. The sixth finding is invented twice over: `critical`
    came from `P0` inside the QUOTED message subject in the heading, and the
    report had in fact honoured the marker — its verdict is *not escalated*.
    """

    def test_only_the_item_the_sentence_is_about_is_reported(self) -> None:
        findings = hedged_items(LIVE_REPORT_B, LIVE_RESULTS)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "takes it back" in findings[0][1]

    def test_the_flagged_item_is_not_filed_under_two_verdicts(self) -> None:
        from robothor.engine.verdict_sections import blocks
        from robothor.engine.verdict_shapes import verdicts_in

        flagged = next(block for block in blocks(LIVE_REPORT_B) if "msg_2209" in block)
        assert verdicts_in(flagged) == {"no-action"}


class TestAClaimIsAboutTheLineItIsOn:
    """A hedge, a hand-back and an override are claims about an item, and they
    were written into every id in their block. In a recap section that is five
    items the sentence was never about."""

    def test_a_subject_block_keeps_its_claims_to_its_subject(self) -> None:
        """The repro: a summary item that mentions another item and hedges in
        the same breath reports itself, never the item it mentions."""
        report = (
            "# Triage\n\n## Low\n\n### 9. Weekly summary — msg_2208\n"
            "- Correlates with msg_2203, and if it is real, escalate immediately.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2208"]

    def test_a_subjectless_block_anchors_the_hedge_to_its_own_line(self) -> None:
        report = (
            "# Triage\n\n## Notes\n\n"
            "1. msg_2201 and msg_2202 are both with Legal.\n"
            "2. msg_2209 was filed as a routing test; if it is real, escalate immediately.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2209"]

    def test_a_hedge_whose_unit_names_nobody_is_dropped(self) -> None:
        """Where there is nothing better to anchor to, the claim is DROPPED
        rather than spread. Reporting it against both items is a fabrication
        against at least one of them, and the cost of the miss is one
        unreported hedge — see `TestAClaimAboutNobodyIsDropped`."""
        report = (
            "# Triage\n\n## Critical\n\n"
            "- msg_2301 and msg_2302 were both escalated.\n"
            "- If this is a routing drill, downgrade both.\n"
        )
        assert hedged_items(report) == []

    def test_a_hand_back_is_anchored_the_same_way(self) -> None:
        report = (
            "# Triage\n\n## Notes\n\n"
            "1. msg_2401 is with Legal.\n"
            "2. msg_2402 — please confirm whether this is a live incident or a drill.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2402"]


class TestAQuotedTitleIsNotAVerdict:
    """A heading that quotes the message's own subject line is naming the item,
    not classifying it. `### 9. "P0 platform outage" …` filed the item Critical
    on the strength of the customer's own words."""

    def _verdicts(self, heading: str, body: str = "") -> set[str]:
        from robothor.engine.verdict_shapes import verdicts_in

        return verdicts_in(f"{heading}\n{body}")

    def test_a_verdict_word_inside_a_quoted_title_is_not_a_label(self) -> None:
        assert self._verdicts('### 9. "P0 platform outage" with QA metadata — msg_2209') == set()

    def test_a_quotation_that_is_only_the_verdict_still_is_one(self) -> None:
        """The other way, and the line between them: a quoted phrase that is a
        verdict and nothing else is a label however it is punctuated."""
        assert self._verdicts('## "Critical"') == {"critical"}
        assert self._verdicts('### 1. An item\n**Severity: "High"**') == {"high"}

    def test_the_flagged_lead_is_still_read(self) -> None:
        assert self._verdicts(
            '### 9. "P0 platform outage" — msg_2209',
            "- **Why it is flagged, not escalated:** its own metadata says so.",
        ) == {"no-action"}


class TestTheShapesOfAnIdentityField:
    def test_a_parenthetical_after_the_id_is_still_an_id_field(self) -> None:
        from robothor.engine.verdict_sections import block_subject

        block = (
            "### 1. SQL injection — @customer-a\n- **Message ID:** msg_2205 (follow-up: msg_2212)\n"
        )
        assert block_subject(block) == "msg_2205"

    def test_a_plural_id_field_names_no_single_subject(self) -> None:
        """Deliberate: `**Message IDs:** msg_2202 / msg_2210` is a block about
        two items, and picking one of them would be the known limit of the
        heading rule with none of its excuse. It falls back to "no subject", so
        the block's verdict reaches both — which is what the report says."""
        from robothor.engine.verdict_sections import block_subject

        block = "### 2. DPA compliance — @customer-b\n- **Message IDs:** msg_2202 / msg_2210\n"
        assert block_subject(block) == ""

    @pytest.mark.parametrize(
        "field",
        [
            "- **Routed to:** msg_2210",
            "- **Duplicate of:** msg_3101",
            "- **Related:** msg_2212",
            "- **Follow-up:** msg_2212",
        ],
        ids=["routed-to", "duplicate-of", "related", "follow-up"],
    )
    def test_a_field_that_names_somebody_else_is_never_the_subject(self, field: str) -> None:
        from robothor.engine.verdict_sections import block_subject

        assert block_subject(f"### 1. An item\n{field}\n") == ""


# ──────────────────────────────────────────────────────────────────────
# Round 3 of the review: a crash, a table, and a claim about nobody
# ──────────────────────────────────────────────────────────────────────


class TestASubjectIsAlwaysOneOfTheBlocksItems:
    """CRASH. `_ID_FIELD` is case-insensitive and its value alternation was
    too, so it read `MSG_2209` and `jira-4412` as subjects — identifiers
    `item_ids` does not produce. The subject then indexed a dict built from
    `item_ids`, raised KeyError inside `inspect_report`, and the
    `contextlib.suppress` in `loop_guards` turned the whole deliverable into a
    silent UNCHECKED. A guardrail that cannot read its input reporting an
    honest zero forever, for the fourth time in this cluster.
    """

    @pytest.mark.parametrize(
        "field",
        ["- **Message ID:** MSG_2209", "- **Ticket:** jira-4412", "- **ID:** Msg_2209"],
        ids=["upper-msg", "lower-ticket", "mixed-case"],
    )
    def test_a_value_the_id_vocabulary_rejects_never_crashes(self, field: str) -> None:
        report = f"# Triage\n\n## Critical\n\n### 1. An outage\n{field}\n- Routed to @owner-a.\n"
        assert hedged_items(report, RESULTS_WITH_MARKER) == []

    @pytest.mark.parametrize(
        "report",
        [
            MEASURED_REPORT,
            FLAT_REPORT,
            LIVE_REPORT,
            LIVE_REPORT_B,
            DOUBLE_VERDICT_REPORT,
            COMMITTED_WITH_MARKER,
            HEDGED_REPORT,
        ],
        ids=["measured", "flat", "live-a", "live-b", "double", "committed", "hedged"],
    )
    def test_every_subject_is_an_item_of_its_own_block(self, report: str) -> None:
        """The invariant the crash violated, over every deliverable fixture in
        this file: a block's subject is one of the ids that block names."""
        from robothor.engine.verdict_sections import block_subject, blocks
        from robothor.engine.verdict_shapes import item_ids

        for block in blocks(report):
            subject = block_subject(block)
            assert subject == "" or subject in item_ids(block), block[:80]


class TestATableRowIsAClaimsUnit:
    """A markdown table has no blank lines and no bullets, so the whole table
    was one unit: a hedge in one cell was attributed to every id in it."""

    TABLE = """# Triage

## Notes

| Message ID | Owner | Note |
|------------|-------|------|
| msg_4101 | @owner-a | Routed and acknowledged. |
| msg_4103 | @owner-b | Filed as a drill; if it is real, escalate immediately. |
| msg_4105 | @owner-c | Closed with the customer. |
"""

    def test_a_hedge_in_one_row_is_about_that_row(self) -> None:
        assert [item for item, _why in hedged_items(self.TABLE)] == ["msg_4103"]

    def test_the_quoted_sentence_stops_at_the_row(self) -> None:
        """And what the model is shown is that row, not the next one: the
        re-ask quotes the sentence the agent has to replace."""
        findings = hedged_items(self.TABLE)
        why = findings[0][1]
        assert "msg_4105" not in why
        assert "|" not in why


class TestAClaimAboutNobodyIsDropped:
    """The block-wide fallback fabricated on its own. A closing paragraph —
    "Overall the week is quiet; if it is real, escalate immediately." — names
    nobody, and in a section listing several items it was reported against all
    of them. A claim whose unit names nobody is attributed only where there is
    exactly one item it could be about."""

    def test_a_closing_paragraph_in_a_multi_item_section_is_dropped(self) -> None:
        report = (
            "# Triage\n\n## Notes\n\n"
            "- msg_4201 went to @owner-a.\n"
            "- msg_4202 went to @owner-b.\n"
            "- msg_4203 went to @owner-c.\n\n"
            "Overall the week is quiet; if it is real, escalate immediately.\n"
        )
        assert hedged_items(report) == []

    def test_a_block_about_one_item_still_takes_the_claim(self) -> None:
        """Fails closed, not silent: where the block names one item there is no
        ambiguity about who the sentence is about."""
        report = (
            "# Triage\n\n## Critical\n\n### 1. An outage\n"
            "- **Message ID:** msg_4204\n"
            "- Routed to @owner-a.\n\n"
            "If this is a routing drill, downgrade it.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_4204"]

    def test_the_drop_is_logged(self, caplog) -> None:
        report = (
            "# Triage\n\n## Notes\n\n"
            "- msg_4205 went to @owner-a.\n"
            "- msg_4206 went to @owner-b.\n\n"
            "If this is a drill, downgrade both.\n"
        )
        with caplog.at_level(logging.DEBUG, logger="robothor.engine.verdict_sections"):
            assert hedged_items(report) == []
        assert "names no item" in caplog.text


class TestTheQuotedSentenceStaysInsideItsUnit:
    def test_the_next_bullet_is_not_quoted_into_the_re_ask(self) -> None:
        report = (
            "# Triage\n\n## Notes\n\n"
            "1. msg_4301 was filed as a drill; if it is real, escalate immediately.\n"
            "2. msg_4302 is a billing question and needs nothing from you.\n"
        )
        findings = hedged_items(report)
        assert [item for item, _why in findings] == ["msg_4301"]
        assert "msg_4302" not in findings[0][1]
        assert "billing" not in findings[0][1]


class TestTheIdVocabularyHasOneDefinition:
    """The KeyError existed because two copies of the same alternation drifted
    apart by one flag. The field pattern is now built out of the vocabulary it
    has to agree with, so the next reader cannot re-create that gap."""

    def test_the_identity_field_is_built_from_the_item_vocabulary(self) -> None:
        from robothor.engine.verdict_sections import _ID_FIELD
        from robothor.engine.verdict_shapes import ITEM_ID

        assert f"(?-i:{ITEM_ID.pattern})" in _ID_FIELD.pattern

    @pytest.mark.parametrize(
        ("field", "subject"),
        [
            ("- **Message ID:** msg_2208", "msg_2208"),
            ("- **Ticket:** ABC-12", "ABC-12"),
            ("- **Message ID:** MSG_2209", ""),
            ("- **Ticket:** jira-4412", ""),
        ],
        ids=["msg", "ticket", "upper-msg", "lower-ticket"],
    )
    def test_the_field_admits_exactly_what_the_vocabulary_admits(
        self, field: str, subject: str
    ) -> None:
        from robothor.engine.verdict_sections import block_subject

        assert block_subject(f"### 1. An item\n{field}\n") == subject
