"""Memories strengthen by the outcome of the runs that used them.

A fact's utility is learned from the runs whose recalls returned it: a run that
failed weakens it, a run that completed gives it a small credit, and a run whose
answer demonstrably used it gives a larger one. Being returned, by itself, adds
nothing. Utility feeds decay and ranking as one signed, bounded term.

These are the pure properties: the utility function, the decay formula with
use that fades, the blend, the trust rule, citation detection, the edge seam,
and the delivery wiring. The database round-trips live in
``test_outcome_utility_integration.py``.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── The utility function ────────────────────────────────────────────────────


class TestUtilityFunction:
    def test_no_evidence_is_the_neutral_prior(self) -> None:
        from robothor.memory.utility import UTILITY_PRIOR, compute_utility

        assert compute_utility(citations=0, failures=0) == pytest.approx(UTILITY_PRIOR)
        assert UTILITY_PRIOR == 0.0

    def test_bounded_for_any_evidence(self) -> None:
        from robothor.memory.utility import UTILITY_MAX, UTILITY_MIN, compute_utility

        grid = [0, 1, 2, 5, 50, 10_000]
        for c, f in itertools.product(grid, grid):
            u = compute_utility(citations=c, failures=f)
            assert UTILITY_MIN <= u <= UTILITY_MAX, (c, f, u)
        assert UTILITY_MIN == -1.0
        assert UTILITY_MAX == 1.0

    def test_strictly_monotone_in_citations(self) -> None:
        from robothor.memory.utility import compute_utility

        for f in (0, 1, 5):
            seq = [compute_utility(citations=c, failures=f) for c in range(0, 30)]
            assert all(b > a for a, b in itertools.pairwise(seq)), f

    def test_strictly_anti_monotone_in_failures(self) -> None:
        from robothor.memory.utility import compute_utility

        for c in (0, 3):
            seq = [compute_utility(citations=c, failures=f) for f in range(0, 30)]
            assert all(b < a for a, b in itertools.pairwise(seq)), c

    def test_a_citation_raises_and_a_failure_lowers_from_neutral(self) -> None:
        from robothor.memory.utility import compute_utility

        assert compute_utility(citations=1, failures=0) > 0.0
        assert compute_utility(citations=0, failures=1) < 0.0

    def test_diminishing_returns(self) -> None:
        from robothor.memory.utility import compute_utility

        ups = [compute_utility(citations=c, failures=0) for c in range(8)]
        downs = [compute_utility(citations=0, failures=f) for f in range(8)]
        for seq in (ups, [-d for d in downs]):
            gains = [b - a for a, b in itertools.pairwise(seq)]
            assert all(b <= a for a, b in itertools.pairwise(gains))

    def test_merely_completing_is_not_evidence_of_use(self) -> None:
        """Criterion 3: a fact returned in many successful runs that never used
        it does not rise, and those runs do not offset its failures either."""
        import inspect

        from robothor.memory.utility import compute_utility, utility_for_row

        assert "successes" not in inspect.signature(compute_utility).parameters
        ridealong = {"outcome_successes": 10_000, "outcome_citations": 0, "outcome_failures": 0}
        assert utility_for_row(ridealong) == 0.0
        blamed = {"outcome_successes": 50, "outcome_citations": 0, "outcome_failures": 1}
        assert utility_for_row(blamed) == compute_utility(citations=0, failures=1)

    def test_cited_use_saturates_toward_the_ceiling(self) -> None:
        from robothor.memory.utility import compute_utility

        assert 0.9 < compute_utility(citations=50, failures=0) < 1.0

    def test_failures_saturate_toward_the_floor(self) -> None:
        from robothor.memory.utility import compute_utility

        assert compute_utility(citations=0, failures=200) > -1.0
        assert compute_utility(citations=0, failures=200) < -0.95

    def test_negative_counts_are_treated_as_zero(self) -> None:
        from robothor.memory.utility import compute_utility

        assert compute_utility(citations=-1, failures=-2) == compute_utility(
            citations=0, failures=0
        )


class TestUtilityDecayTerm:
    def test_neutral_is_zero_and_signs_follow_utility(self) -> None:
        from robothor.memory.utility import utility_decay_term

        assert utility_decay_term(0.0) == 0.0
        assert utility_decay_term(0.5) > 0.0
        assert utility_decay_term(-0.5) < 0.0

    def test_failure_side_keeps_the_existing_cap(self) -> None:
        """The failure penalty was capped at 0.4 so one bad streak cannot
        destroy a fact outright; the signed term keeps that cap."""
        from robothor.memory.utility import MAX_FAILURE_PENALTY, utility_decay_term

        assert MAX_FAILURE_PENALTY == 0.4
        for u in (-1.0, -5.0, -0.999):
            assert utility_decay_term(u) >= -MAX_FAILURE_PENALTY
        assert utility_decay_term(-1.0) == pytest.approx(-MAX_FAILURE_PENALTY)

    def test_failure_penalty_is_never_weaker_than_the_linear_one_it_replaced(self) -> None:
        """The old penalty was 0.1 per failure, capped at 0.4 (reached at 4)."""
        from robothor.memory.utility import compute_utility, utility_decay_term

        for f in range(0, 30):
            penalty = -utility_decay_term(compute_utility(citations=0, failures=f))
            assert penalty >= min(0.1 * f, 0.4) - 1e-9, (f, penalty)
            assert penalty <= 0.4 + 1e-9
        assert -utility_decay_term(compute_utility(citations=0, failures=4)) == pytest.approx(0.4)

    def test_positive_side_is_bounded(self) -> None:
        from robothor.memory.utility import MAX_UTILITY_BOOST, utility_decay_term

        assert utility_decay_term(1.0) == pytest.approx(MAX_UTILITY_BOOST)
        assert utility_decay_term(7.0) == pytest.approx(MAX_UTILITY_BOOST)

    def test_monotone(self) -> None:
        from robothor.memory.utility import utility_decay_term

        grid = [x / 20 for x in range(-20, 21)]
        terms = [utility_decay_term(u) for u in grid]
        assert all(b >= a for a, b in itertools.pairwise(terms))


# ── Trust: lower authority earns no reinforcement ───────────────────────────


class TestOriginEligibility:
    def test_a_row_without_an_origin_column_is_eligible(self) -> None:
        from robothor.memory.utility import earns_positive_utility

        assert earns_positive_utility({"id": 1, "fact_text": "x"}) is True

    def test_a_null_origin_is_eligible(self) -> None:
        from robothor.memory.utility import earns_positive_utility

        assert earns_positive_utility({"origin": None}) is True

    @pytest.mark.parametrize("origin", ["external", "inferred", "EXTERNAL"])
    def test_external_and_inferred_earn_nothing_positive(self, origin: str) -> None:
        from robothor.memory.utility import earns_positive_utility

        assert earns_positive_utility({"origin": origin}) is False

    @pytest.mark.parametrize(
        "origin", ["operator_confirmed", "user_stated", "crm", "tool", "agent_generated"]
    )
    def test_trusted_origins_are_eligible(self, origin: str) -> None:
        from robothor.memory.utility import earns_positive_utility

        assert earns_positive_utility({"origin": origin}) is True

    def test_utility_for_row_clamps_ineligible_rows_at_neutral_but_keeps_penalties(
        self,
    ) -> None:
        from robothor.memory.utility import utility_for_row

        good = {"outcome_successes": 5, "outcome_citations": 5, "outcome_failures": 0}
        bad = {"outcome_successes": 0, "outcome_citations": 0, "outcome_failures": 3}
        assert utility_for_row(good) > 0.0
        assert utility_for_row({**good, "origin": "external"}) == 0.0
        assert utility_for_row({**bad, "origin": "external"}) < 0.0
        assert utility_for_row({**bad, "origin": "external"}) == utility_for_row(bad)

    def test_utility_for_row_reads_nulls_as_zero(self) -> None:
        from robothor.memory.utility import utility_for_row

        assert (
            utility_for_row(
                {"outcome_successes": None, "outcome_citations": None, "outcome_failures": None}
            )
            == 0.0
        )


# ── "Demonstrably used": a distinctive value of the fact in the answer ─────


class TestCitationDetection:
    def test_a_distinctive_value_in_the_answer_is_a_citation(self) -> None:
        from robothor.memory.utility import is_cited

        fact = "Invoice INV-20417 for Alice was paid on 2026-03-14."
        assert is_cited(fact, "The invoice INV-20417 is settled.", task_text="Is it paid?")

    def test_an_email_or_url_counts(self) -> None:
        from robothor.memory.utility import is_cited

        assert is_cited("Bob's address is bob@example.com.", "Write to bob@example.com", "")
        assert is_cited("Docs live at https://docs.example.com/x", "see docs.example.com/x", "")

    def test_shared_ordinary_words_are_not_a_citation(self) -> None:
        from robothor.memory.utility import is_cited

        fact = "Alice prefers the quarterly report as a PDF."
        assert not is_cited(fact, "Alice wants the quarterly report as a PDF.", "")

    def test_a_value_the_task_already_stated_is_not_a_citation(self) -> None:
        """Echoing the prompt is not evidence the memory was used."""
        from robothor.memory.utility import is_cited

        fact = "Invoice INV-20417 for Alice was paid."
        assert not is_cited(fact, "INV-20417 noted.", task_text="What about INV-20417?")

    def test_a_bare_year_or_short_number_is_not_distinctive(self) -> None:
        from robothor.memory.utility import distinctive_tokens

        assert distinctive_tokens("In 2026 Bob had 12 meetings.") == set()

    def test_a_short_bare_number_is_not_distinctive(self) -> None:
        from robothor.memory.utility import distinctive_tokens, is_cited

        assert not is_cited("The office has 120 desks.", "Processed 120 emails today.", "")
        assert distinctive_tokens("Budget 1000, then 25000 later.") == set()

    def test_a_long_bare_number_is_distinctive(self) -> None:
        from robothor.memory.utility import is_cited

        assert is_cited("Alice's account number is 4417203.", "Account 4417203 is open.", "")

    def test_clock_times_and_calendar_dates_are_not_distinctive(self) -> None:
        """Digests print today's date and times; a fact holding them is not used."""
        from robothor.memory.utility import distinctive_tokens

        text = "Standup at 9:30, review 10:00, due 11/25, shipped 2026-11-24 and 24.11.2026."
        assert distinctive_tokens(text) == set()

    def test_matching_is_whole_token_and_case_insensitive(self) -> None:
        from robothor.memory.utility import is_cited

        assert is_cited("Ticket ab-4411 is open.", "AB-4411 closed", "")
        assert not is_cited("Ticket ab-4411 is open.", "AB-44117 closed", "")

    def test_empty_output_cites_nothing(self) -> None:
        from robothor.memory.utility import is_cited

        assert not is_cited("Invoice INV-20417.", "", "")
        assert not is_cited("Invoice INV-20417.", None, None)


# ── Edge utility seam (wired by the graph lane at integration) ─────────────


class TestEdgeUtilitySeam:
    def test_no_change_moves_no_weight(self) -> None:
        from robothor.memory.utility import edge_weight_delta

        assert edge_weight_delta(0.3, 0.3) == 0.0

    def test_delta_follows_the_sign_and_is_bounded(self) -> None:
        from robothor.memory.utility import MAX_EDGE_WEIGHT_DELTA, edge_weight_delta

        assert edge_weight_delta(0.0, 0.5) > 0.0
        assert edge_weight_delta(0.0, -0.5) < 0.0
        assert abs(edge_weight_delta(-1.0, 1.0)) <= MAX_EDGE_WEIGHT_DELTA
        assert abs(edge_weight_delta(1.0, -1.0)) <= MAX_EDGE_WEIGHT_DELTA

    def test_apply_keeps_weights_in_range_and_is_pure(self) -> None:
        from robothor.memory.utility import EDGE_WEIGHT_MAX, EDGE_WEIGHT_MIN, apply_edge_utility

        weights = {"e1": 1.0, "e2": EDGE_WEIGHT_MAX, "e3": EDGE_WEIGHT_MIN}
        up = apply_edge_utility(weights, utility_before=0.0, utility_after=1.0)
        down = apply_edge_utility(weights, utility_before=0.0, utility_after=-1.0)
        assert weights == {"e1": 1.0, "e2": EDGE_WEIGHT_MAX, "e3": EDGE_WEIGHT_MIN}
        assert up["e1"] > 1.0 > down["e1"]
        assert up["e2"] == EDGE_WEIGHT_MAX
        assert down["e3"] == EDGE_WEIGHT_MIN
        for w in (*up.values(), *down.values()):
            assert EDGE_WEIGHT_MIN <= w <= EDGE_WEIGHT_MAX


# ── Decay with use that fades ───────────────────────────────────────────────


def _decay(**kw: Any) -> float:
    from robothor.memory.lifecycle import compute_decay_score

    base: dict[str, Any] = {
        "last_accessed": datetime.now(UTC) - timedelta(days=1),
        "access_count": 0,
        "reinforcement_count": 0,
        "importance_score": 0.2,
    }
    base.update(kw)
    return compute_decay_score(**base)


class TestDecayWithFadingUse:
    def test_the_access_boost_fades_with_time_since_last_use(self) -> None:
        from robothor.memory.lifecycle import ACCESS_BOOST_HALF_LIFE_DAYS, access_boost

        assert ACCESS_BOOST_HALF_LIFE_DAYS == 30.0
        fresh = access_boost(5, days_since_use=0)
        assert access_boost(5, days_since_use=30) == pytest.approx(fresh / 2)
        assert access_boost(5, days_since_use=400) < 0.001
        assert access_boost(0, days_since_use=0) == 0.0

    def test_one_recall_long_ago_leaves_a_fact_where_it_would_be_unrecalled(self) -> None:
        old = datetime.now(UTC) - timedelta(days=400)
        once = _decay(last_accessed=old, access_count=1)
        never = _decay(last_accessed=old, access_count=0)
        assert once == pytest.approx(never, abs=1e-3)
        assert once < 0.1

    def test_a_recall_last_week_still_counts(self) -> None:
        week = datetime.now(UTC) - timedelta(days=7)
        assert _decay(last_accessed=week, access_count=1) > _decay(
            last_accessed=week, access_count=0
        )

    def test_positive_utility_raises_and_negative_lowers_decay(self) -> None:
        old = datetime.now(UTC) - timedelta(days=400)
        neutral = _decay(last_accessed=old, utility=0.0)
        assert _decay(last_accessed=old, utility=0.8) > neutral + 0.1
        assert _decay(last_accessed=old, utility=-0.8) <= neutral

    def test_utility_replaces_the_failure_only_penalty(self) -> None:
        """One signed term: given a utility, outcome_failures is not also
        subtracted (they would double count the same failures)."""
        mid = datetime.now(UTC) - timedelta(hours=72)
        a = _decay(last_accessed=mid, importance_score=0.5, utility=-0.25, outcome_failures=3)
        b = _decay(last_accessed=mid, importance_score=0.5, utility=-0.25, outcome_failures=0)
        assert a == pytest.approx(b, abs=1e-4)

    def test_failures_alone_still_lower_the_score_and_respect_the_cap(self) -> None:
        mid = datetime.now(UTC) - timedelta(hours=72)
        healthy = _decay(last_accessed=mid, importance_score=0.5)
        wounded = _decay(last_accessed=mid, importance_score=0.5, outcome_failures=200)
        assert wounded < healthy
        assert healthy - wounded <= 0.4 + 1e-6


# ── Ranking: utility takes over raw access count's weight ──────────────────


def _row(fid: int, rel: float, utility: float | None = None, access: int = 0) -> dict[str, Any]:
    return {
        "id": fid,
        "fact_text": f"distinct fact number {fid} about topic {fid * 7}",
        "category": "event",
        "entities": [],
        "importance_score": 0.5,
        "access_count": access,
        "utility": utility,
        "age_seconds": 3600.0,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "rerank_score": rel,
    }


def _order(rows: list[dict[str, Any]]) -> list[int]:
    from robothor.memory.facts import _blend_rank

    with (
        patch("robothor.memory.facts._dedup_enabled", return_value=False),
        patch("robothor.memory.facts._temporal_coherence_enabled", return_value=False),
        patch("robothor.memory.facts._rerank_wide_enabled", return_value=False),
    ):
        return [r["id"] for r in _blend_rank(rows, 10)]


class TestBlendUtility:
    def test_equal_relevance_higher_utility_ranks_first(self) -> None:
        # Higher id would win the newest-wins tie-break, so put utility on the lower id.
        assert _order([_row(1, 0.7, utility=0.4), _row(2, 0.7, utility=0.0)]) == [1, 2]
        assert _order([_row(1, 0.7, utility=0.0), _row(2, 0.7, utility=-0.4)]) == [1, 2]

    def test_raw_access_count_no_longer_ranks(self) -> None:
        """Retrieval popularity fed ranking, which raised retrieval: a
        rich-get-richer loop. Access count alone now changes nothing."""
        assert _order([_row(1, 0.7, access=500), _row(2, 0.7, access=0)]) == [2, 1]

    def test_the_utility_weight_takes_over_the_access_weight(self) -> None:
        from robothor.memory.facts import BLEND_WEIGHTS

        assert BLEND_WEIGHTS["utility"] <= 0.05 + 0.02
        assert "access" not in BLEND_WEIGHTS
        assert BLEND_WEIGHTS["relevance"] == max(BLEND_WEIGHTS.values())

    def test_no_utility_lifts_a_fact_over_one_more_relevant_by_the_margin(self) -> None:
        from robothor.memory.facts import UTILITY_RANK_MARGIN

        assert UTILITY_RANK_MARGIN < 0.1
        for lo in (0.1, 0.3, 0.5, 0.8):
            hi = min(1.0, lo + UTILITY_RANK_MARGIN + 1e-6)
            for u_lo, u_hi in ((1.0, -1.0), (1.0, 0.0), (0.5, -0.5)):
                # The more relevant fact gets the LOWER id, so ties favour the other.
                rows = [_row(1, hi, utility=u_hi), _row(2, lo, utility=u_lo)]
                assert _order(rows)[0] == 1, (lo, hi, u_lo, u_hi)

    def test_inside_the_margin_utility_can_decide(self) -> None:
        from robothor.memory.facts import UTILITY_RANK_MARGIN

        lo, hi = 0.5, 0.5 + UTILITY_RANK_MARGIN / 2
        assert _order([_row(1, hi, utility=-1.0), _row(2, lo, utility=1.0)])[0] == 2

    def test_a_missing_utility_is_neutral(self) -> None:
        assert _order([_row(1, 0.7, utility=None), _row(2, 0.7, utility=0.0)]) == [2, 1]


# ── Delivery: credit is assigned when a run ends ────────────────────────────


def _run(**kw: Any) -> Any:
    from robothor.engine.models import AgentRun, RunStatus

    run = AgentRun(agent_id="worker", tenant_id="test-tenant")
    run.status = kw.pop("status", RunStatus.COMPLETED)
    run.output_text = kw.pop("output_text", "Invoice INV-20417 is paid.")
    for k, v in kw.items():
        setattr(run, k, v)
    return run


async def _deliver(run: Any) -> tuple[MagicMock, MagicMock]:
    from robothor.engine.delivery import deliver
    from robothor.engine.models import AgentConfig, DeliveryMode

    config = AgentConfig(id="worker", name="worker", delivery_mode=DeliveryMode.NONE)
    credit = MagicMock(return_value={})
    bump = MagicMock(return_value={})
    with (
        patch("robothor.memory.outcomes.credit_success_for_run", credit),
        patch("robothor.memory.outcomes.bump_failure_for_run", bump),
        patch("robothor.engine.delivery._persist_delivery_status", AsyncMock()),
    ):
        await deliver(config, run)
    return credit, bump


class TestRunOutcomeClassification:
    def test_only_a_completed_run_with_output_and_no_error_succeeded(self) -> None:
        from robothor.engine.models import RunStatus
        from robothor.engine.run_outcome_attribution import run_succeeded

        assert run_succeeded(_run()) is True
        assert run_succeeded(_run(output_text="")) is False
        assert run_succeeded(_run(error_message="boom")) is False
        assert run_succeeded(_run(budget_exhausted=True)) is False
        for status in (
            RunStatus.FAILED,
            RunStatus.TIMEOUT,
            RunStatus.CANCELLED,
            RunStatus.SKIPPED,
            RunStatus.RUNNING,
            RunStatus.PENDING,
            RunStatus.AWAITING_APPROVAL,
        ):
            assert run_succeeded(_run(status=status)) is False, status


class TestDeliveryAssignsCredit:
    @pytest.mark.asyncio
    async def test_a_successful_run_credits_what_it_recalled(self) -> None:
        run = _run(task_text="Is the invoice paid?")
        credit, bump = await _deliver(run)
        credit.assert_called_once()
        args, kwargs = credit.call_args
        assert args[0] == str(run.id)
        assert args[1] == "test-tenant"
        assert kwargs["output_text"] == "Invoice INV-20417 is paid."
        assert kwargs["task_text"] == "Is the invoice paid?"
        bump.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_run_penalises_and_earns_no_credit(self) -> None:
        from robothor.engine.models import RunStatus

        credit, bump = await _deliver(_run(status=RunStatus.FAILED, error_message="boom"))
        bump.assert_called_once()
        credit.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cancelled_run_neither_credits_nor_penalises(self) -> None:
        from robothor.engine.models import RunStatus

        credit, bump = await _deliver(_run(status=RunStatus.CANCELLED))
        credit.assert_not_called()
        bump.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_benchmark_run_earns_no_credit(self) -> None:
        credit, _ = await _deliver(_run(is_benchmark=True))
        credit.assert_not_called()
