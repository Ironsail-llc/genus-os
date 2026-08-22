"""A recurring metric must not contradict what the same agent said last time.

The 2026-08-22 morning briefing ended with:

    Fleet health: 52.8% (↓0.5pp WoW)

Its own previous briefings had said 48.6% (Aug 21) and 57.8% (Aug 18). So the
real change was **+4.2pp** and the delivered claim was a *decrease* of half a
point. Both the value and the trend were unsourced: the run's tool output
carried no fleet-health field and was truncated at 9,749 chars, and no
historical query ran.

A broader detector was tried first and REJECTED on measurement: "is this number
present anywhere in the run's tool outputs" both missed this case (52.8 appears
by coincidence inside a large JSON blob) and flagged 15 of 21 legitimate numbers
on other days. A control with that error rate certifies nothing.

What is checkable without ambiguity is self-consistency: when an agent publishes
a labelled metric with a direction, and it published the same label before, the
direction must agree with its own history. No tool trace required, no arithmetic
guesswork — only the agent's own words, twice.

This catches the operator-facing failure that matters: a confident trend that
contradicts the record.
"""

from __future__ import annotations

from robothor.engine.stat_verification import (
    MetricClaim,
    check_trend_consistency,
    extract_metric_claims,
)

TODAY = "Fleet health: 52.8% (↓0.5pp WoW)"
YESTERDAY = "Fleet health: 48.6% (↑1.2pp WoW)"


class TestExtraction:
    def test_extracts_label_value_and_signed_delta(self) -> None:
        claims = extract_metric_claims(TODAY)
        assert len(claims) == 1
        c = claims[0]
        assert c.label == "fleet health"
        assert c.value == 52.8
        assert c.delta == -0.5

    def test_up_arrow_is_positive(self) -> None:
        c = extract_metric_claims("Fleet health: 48.6% (↑1.2pp WoW)")[0]
        assert c.delta == 1.2

    def test_words_work_as_well_as_arrows(self) -> None:
        c = extract_metric_claims("Fleet health: 52.8% (down 0.5pp week-over-week)")[0]
        assert c.delta == -0.5

    def test_a_plain_metric_with_no_trend_has_no_delta(self) -> None:
        c = extract_metric_claims("Fleet health: 52.8%")[0]
        assert c.delta is None

    def test_ignores_prose_numbers(self) -> None:
        assert extract_metric_claims("We looked at 52.8 things today.") == []

    def test_handles_multiple_metrics(self) -> None:
        text = "Uptime: 99.1% (↑0.2pp)\nFleet health: 52.8% (↓0.5pp WoW)"
        labels = {c.label for c in extract_metric_claims(text)}
        assert labels == {"uptime", "fleet health"}


class TestTrendConsistency:
    def test_the_real_case_is_caught(self) -> None:
        """48.6 -> 52.8 is +4.2pp. The briefing claimed a 0.5pp DECREASE."""
        bad = check_trend_consistency(extract_metric_claims(TODAY), extract_metric_claims(YESTERDAY))
        assert len(bad) == 1
        assert bad[0].label == "fleet health"
        assert "+4.2" in bad[0].detail and "-0.5" in bad[0].detail

    def test_an_honest_trend_passes(self) -> None:
        now = extract_metric_claims("Fleet health: 52.8% (↑4.2pp WoW)")
        prev = extract_metric_claims(YESTERDAY)
        assert check_trend_consistency(now, prev) == []

    def test_rounding_is_tolerated(self) -> None:
        """A tenth of a point of rounding is not a lie."""
        now = extract_metric_claims("Fleet health: 52.8% (↑4.1pp)")
        prev = extract_metric_claims(YESTERDAY)
        assert check_trend_consistency(now, prev) == []

    def test_no_previous_publication_is_not_a_violation(self) -> None:
        """Absence of history is not evidence of a lie — say nothing."""
        assert check_trend_consistency(extract_metric_claims(TODAY), []) == []

    def test_a_metric_with_no_claimed_trend_is_not_checked(self) -> None:
        now = extract_metric_claims("Fleet health: 52.8%")
        prev = extract_metric_claims(YESTERDAY)
        assert check_trend_consistency(now, prev) == []

    def test_only_matching_labels_are_compared(self) -> None:
        now = extract_metric_claims("Uptime: 52.8% (↓0.5pp)")
        prev = extract_metric_claims(YESTERDAY)
        assert check_trend_consistency(now, prev) == []

    def test_sign_contradiction_is_reported_even_when_small(self) -> None:
        """Direction is the claim a reader actually acts on."""
        now = extract_metric_claims("Fleet health: 49.0% (↓0.4pp)")
        prev = extract_metric_claims(YESTERDAY)
        bad = check_trend_consistency(now, prev)
        assert len(bad) == 1, "claimed a fall while the number rose"


class TestPayload:
    def test_violation_is_json_safe(self) -> None:
        bad = check_trend_consistency(extract_metric_claims(TODAY), extract_metric_claims(YESTERDAY))
        payload = bad[0].to_payload()
        assert payload["label"] == "fleet health"
        assert isinstance(payload["claimed_delta"], float)
        assert isinstance(payload["actual_delta"], float)

    def test_claim_is_hashable_for_dedupe(self) -> None:
        c = extract_metric_claims(TODAY)[0]
        assert isinstance(hash(c), int)
        assert isinstance(c, MetricClaim)
