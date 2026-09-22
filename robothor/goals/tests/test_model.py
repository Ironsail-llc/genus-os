from datetime import UTC, datetime, timedelta

import pytest

from robothor.goals.model import (
    DEFAULT_COST_BUDGET_USD,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TOKEN_BUDGET,
    CreateGoal,
    GoalUpdate,
    exceeded,
    new_goal,
    tokens_affordable,
    transition,
)


def goal(**kwargs):
    return new_goal(
        CreateGoal(
            objective="Prepare the report", success_criteria=["Report is delivered"], **kwargs
        ),
        "operator",
    )


def update(g, action, **kwargs):
    return transition(g, GoalUpdate(action=action, version=g["version"], **kwargs), operator=True)


def test_completion_requires_observed_criteria_not_git():
    g = goal()
    with pytest.raises(ValueError, match="unsatisfied"):
        update(g, "complete", note="Done")
    g = update(
        g,
        "evidence",
        criterion=0,
        reference="artifact:report",
        satisfied=True,
        note="Verified report receipt",
    )
    g = update(g, "complete", note="Delivered")
    assert g["status"] == "review"
    assert update(g, "approve")["status"] == "complete"


def test_failed_tests_and_stale_evidence_cannot_complete():
    g = goal()
    with pytest.raises(ValueError, match="failed test"):
        update(
            g, "evidence", criterion=0, reference="pytest:failed:1", satisfied=True, note="Failure"
        )
    g = update(g, "evidence", criterion=0, reference="artifact:report", satisfied=True, note="Seen")
    g = update(
        g,
        "evidence",
        criterion=0,
        reference="artifact:report",
        satisfied=False,
        note="Receipt withdrawn",
    )
    with pytest.raises(ValueError, match="unsatisfied"):
        update(g, "complete", note="Done")


def test_wait_promotes_only_standalone_short_goal():
    wake = datetime.now(UTC) + timedelta(days=1)
    g = update(goal(), "wait", wake_at=wake, note="Wait for response")
    assert g["kind"] == "long" and g["status"] == "waiting"
    assert g["wait"]["wake_at"] == wake.isoformat()
    child = update(
        goal(parent_goal_id="11111111-1111-4111-8111-111111111111"),
        "wait",
        note="Wait for response",
    )
    assert child["kind"] == "short"


def test_three_repeated_blockers_stop_but_progress_resets():
    g = goal()
    for _ in range(2):
        g = update(g, "block", note="Missing access")
    assert g["status"] == "queued"
    g = update(g, "progress", note="Obtained access", next_action="Read report")
    assert g["blocker_count"] == 0
    for _ in range(3):
        g = update(g, "block", note="Missing report")
    assert g["status"] == "blocked"


def test_pause_stale_updates_and_operator_resume():
    g = goal()
    with pytest.raises(ValueError, match="stale"):
        transition(g, GoalUpdate(action="pause", version=2))
    g = update(g, "pause")
    with pytest.raises(ValueError, match="inactive"):
        update(g, "progress", note="Ignore pause")
    with pytest.raises(ValueError, match="operator"):
        transition(g, GoalUpdate(action="resume", version=g["version"]))
    assert update(g, "resume")["status"] == "queued"


def test_budget_requires_renewal():
    g = goal(token_budget=100)
    g.update(status="blocked", tokens_used=101)
    with pytest.raises(ValueError, match="increase"):
        update(g, "resume")
    assert update(g, "resume", token_budget=200)["status"] == "queued"


def test_ongoing_assessment_needs_fresh_evidence_each_period():
    g = goal(kind="long", mode="ongoing")
    with pytest.raises(ValueError, match="assessed"):
        update(g, "complete", note="Done")
    g = update(
        g, "evidence", criterion=0, reference="metric:today", satisfied=True, note="Measured"
    )
    g = update(g, "assess", assessment="meeting", note="Target met today")
    assert g["status"] == "review" and g["assessment"]["status"] == "meeting"
    g = update(g, "approve")
    assert g["status"] == "waiting" and g["assessment"]["approved_by_operator"]
    assert not g["evidence"]
    with pytest.raises(ValueError, match="requires evidence"):
        update(g, "assess", assessment="meeting", note="Still met")


def test_human_review_and_recovery():
    g = goal(human_review=True)
    g = update(g, "evidence", criterion=0, reference="receipt:1", satisfied=True, note="Verified")
    g["recovery_required"] = True
    with pytest.raises(ValueError, match="reconcile"):
        update(g, "complete", note="Done")
    g = update(g, "reconciled", note="Inspected prior ledger; report delivered once")
    g = update(g, "complete", note="Delivered")
    assert g["status"] == "review"
    assert update(g, "approve")["status"] == "complete"


@pytest.mark.parametrize(
    "kwargs",
    [{"objective": " "}, {"success_criteria": [" "]}, {"mode": "ongoing"}, {"token_budget": 0}],
)
def test_invalid_contracts(kwargs):
    data = {"objective": "Report", "success_criteria": ["Delivered"]}
    data.update(kwargs)
    with pytest.raises(ValueError):
        CreateGoal(**data)


def test_every_goal_is_born_with_a_ceiling():
    """The caller may omit every limit; the goal must still be bounded."""
    g = goal()
    assert g["token_budget"] == DEFAULT_TOKEN_BUDGET
    assert g["cost_budget_usd"] == DEFAULT_COST_BUDGET_USD
    assert g["max_attempts"] == DEFAULT_MAX_ATTEMPTS
    assert datetime.fromisoformat(g["deadline_at"]) > datetime.now(UTC)
    assert not exceeded(g)


@pytest.mark.parametrize(
    "field,hit,raised",
    [
        ("tokens_used", DEFAULT_TOKEN_BUDGET, {"token_budget": DEFAULT_TOKEN_BUDGET * 2}),
        ("cost_usd", DEFAULT_COST_BUDGET_USD, {"cost_budget_usd": DEFAULT_COST_BUDGET_USD * 2}),
        ("attempts", DEFAULT_MAX_ATTEMPTS, {"max_attempts": DEFAULT_MAX_ATTEMPTS * 2}),
    ],
)
def test_each_ceiling_blocks_and_only_raising_it_resumes(field, hit, raised):
    g = goal()
    g[field] = hit
    assert exceeded(g)
    g["status"] = "blocked"
    with pytest.raises(ValueError, match="increase"):
        update(g, "resume")
    assert update(g, "resume", **raised)["status"] == "queued"


def test_deadline_blocks_and_is_extended_not_ignored():
    g = goal(deadline_seconds=3600)
    assert datetime.fromisoformat(g["deadline_at"]) < datetime.now(UTC) + timedelta(hours=2)
    g["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert "deadline" in exceeded(g)
    g["status"] = "blocked"
    with pytest.raises(ValueError, match="increase"):
        update(g, "resume")
    assert update(g, "resume", deadline_seconds=3600)["status"] == "queued"


def test_the_token_cap_for_a_run_respects_the_money_left():
    """Tokens and dollars are not the same ceiling.

    The in-run cap was derived from tokens alone, so a fresh goal's FIRST run
    could spend the whole 1,000,000 before anything consulted the $5.00. At
    $15/M output that is $15 — three times the entire cost ceiling.
    """
    g = goal()
    assert g["cost_budget_usd"] == DEFAULT_COST_BUDGET_USD
    # $15/M output: the money runs out at 333,333 tokens, well inside 1M.
    assert tokens_affordable(g, 0.000_015) == 333_333
    # Spend most of it and the next run's cap shrinks with it.
    g["cost_usd"] = 4.5
    assert tokens_affordable(g, 0.000_015) == 33_333
    # A free model prices no ceiling, so the token budget is the only one.
    assert tokens_affordable(g, 0.0) is None
    # Never negative.
    g["cost_usd"] = 99.0
    assert tokens_affordable(g, 0.000_015) == 0


def test_a_goal_stored_before_ceilings_existed_is_still_bounded():
    """Rows written by the pre-ceiling build carry none of these keys."""
    legacy = {
        k: v
        for k, v in goal().items()
        if k not in {"token_budget", "cost_budget_usd", "max_attempts", "deadline_at"}
    }
    legacy["attempts"] = DEFAULT_MAX_ATTEMPTS
    assert "attempt" in exceeded(legacy)
