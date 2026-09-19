"""An independent assessment can disagree with researcher labels, not invent sources."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from robothor.sales.models import Dossier, Evidence, QualificationPolicy
from robothor.sales.qualification import (
    QualificationAssessment,
    assessment_context,
    evaluate_assessment,
)


def inputs():
    dossier = Dossier(
        buying_case="network",
        evidence=[
            Evidence(
                id="menu",
                field="prescribing",
                value=True,
                url="https://clinic.example.com/services",
                excerpt="Wellness consultations",
                retrieved_at=datetime.now(UTC),
            )
        ],
        criteria={"prescribing": ["menu"]},
    )
    policy = QualificationPolicy(
        version="1",
        buying_case="network",
        required=["prescribing"],
        weights={"prescribing": 100},
        threshold=80,
        criteria_definitions={"prescribing": "Explicit prescription care and a prescribing team."},
    )
    return dossier, policy


def assessment(status="unknown", ids=None):
    return QualificationAssessment.model_validate(
        {
            "criteria": {
                "prescribing": {
                    "status": status,
                    "evidence_ids": ["menu"] if ids is None else ids,
                    "explanation": "A wellness service label does not establish prescription care.",
                }
            },
            "research_gaps": ["Find an explicit prescription care description."],
        }
    )


def test_independent_unknown_overrides_claim_without_rewriting_dossier():
    dossier, policy = inputs()
    original = dossier.model_dump(mode="json")
    assert policy.evaluate(dossier)["score"] == 100
    result = evaluate_assessment(policy, dossier, assessment())
    assert result["score"] == 0
    assert result["decision"] == "needs_research"
    assert result["disproved"] == []
    assert dossier.model_dump(mode="json") == original


@pytest.mark.parametrize(
    "status,decision,score", [("supported", "qualified", 100), ("disproved", "rejected", 0)]
)
def test_assessor_can_disagree_with_original_labels(status, decision, score):
    dossier, policy = inputs()
    dossier.evidence[0].value = "unclassified passage"
    dossier.evidence[0].confidence = "inferred"
    result = evaluate_assessment(policy, dossier, assessment(status))
    assert (result["decision"], result["score"]) == (decision, score)


@pytest.mark.parametrize("change", ["invented", "missing", "extra", "empty", "duplicate"])
def test_incomplete_or_unbound_assessments_are_rejected(change):
    dossier, policy = inputs()
    data = assessment("supported").model_dump()
    if change == "invented":
        data["criteria"]["prescribing"]["evidence_ids"] = ["invented"]
    elif change == "missing":
        data["criteria"] = {}
    elif change == "extra":
        data["criteria"]["extra"] = data["criteria"]["prescribing"]
    elif change == "empty":
        data["criteria"]["prescribing"]["evidence_ids"] = []
    else:
        data["criteria"]["prescribing"]["evidence_ids"] = ["menu", "menu"]
    with pytest.raises(ValueError):
        evaluate_assessment(policy, dossier, QualificationAssessment.model_validate(data))


@pytest.mark.parametrize("time_change", ["old", "future", "expired"])
def test_stale_evidence_cannot_earn_points(time_change):
    dossier, policy = inputs()
    now = datetime.now(UTC)
    if time_change == "old":
        dossier.evidence[0].retrieved_at = now - timedelta(days=91)
    elif time_change == "future":
        dossier.evidence[0].retrieved_at = now + timedelta(minutes=1)
    else:
        dossier.evidence[0].expires_at = now - timedelta(seconds=1)
    assert (
        evaluate_assessment(policy, dossier, assessment("supported"), now=now)["decision"]
        == "needs_research"
    )


def test_model_does_not_set_score_or_receive_researcher_boolean_as_premise():
    dossier, policy = inputs()
    with pytest.raises(ValidationError):
        QualificationAssessment.model_validate({**assessment().model_dump(), "score": 100})
    context = assessment_context(dossier, policy)
    assert set(context["evidence"][0]) == {
        "id",
        "url",
        "excerpt",
        "retrieved_at",
        "published_at",
        "expires_at",
    }
    assert "criteria" not in context
    assert context["policy"]["criteria_definitions"] == policy.criteria_definitions
