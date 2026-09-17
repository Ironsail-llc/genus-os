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

        monkeypatch.setattr(module, "hedged_items", _boom)
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
