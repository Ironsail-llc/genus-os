from datetime import UTC, datetime, timedelta

import pytest

from robothor.goals.model import CreateGoal, GoalUpdate, new_goal, transition


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
    assert update(g, "complete", note="Delivered")["status"] == "complete"


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
    assert g["status"] == "waiting" and g["assessment"]["status"] == "meeting"
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
