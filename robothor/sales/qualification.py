"""Independent passage assessment; policy code remains the only score calculator."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from robothor.sales.models import Contract, Dossier, Evidence, QualificationPolicy


class CriterionAssessment(Contract):
    status: Literal["supported", "disproved", "unknown"]
    evidence_ids: list[str] = Field(max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def valid_references(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Assessment evidence references must be unique")
        if self.status != "unknown" and not self.evidence_ids:
            raise ValueError("Decisive assessments require captured evidence")
        if not self.explanation.strip():
            raise ValueError("Assessment explanation required")
        return self


class QualificationAssessment(Contract):
    criteria: dict[str, CriterionAssessment]
    research_gaps: list[str] = Field(max_length=100)


def assessment_context(dossier: Dossier, policy: QualificationPolicy) -> dict[str, Any]:
    """Supply passages and policy without anchoring on the researcher's labels."""
    return {
        "buying_case": dossier.buying_case,
        "policy": policy.model_dump(mode="json"),
        "evidence": [
            e.model_dump(mode="json", exclude={"field", "value", "confidence"})
            for e in dossier.evidence
        ],
    }


def evaluate_assessment(
    policy: QualificationPolicy,
    dossier: Dossier,
    assessment: QualificationAssessment,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score a transient reviewed view; retain the original dossier unchanged.

    Assessment references may cross researcher field labels. A source can support
    more than one criterion, but it cannot earn points after it expires. Unknown
    means insufficient evidence, not evidence that the criterion is false.
    """
    if set(assessment.criteria) != set(policy.weights):
        raise ValueError("Assessment must cover exactly the published policy criteria")
    facts = {e.id: e for e in dossier.evidence}
    reviewed: list[Evidence] = []
    references: dict[str, list[str]] = {}
    for criterion, finding in assessment.criteria.items():
        if not set(finding.evidence_ids) <= facts.keys():
            raise ValueError("Assessment references unavailable evidence")
        if finding.status == "unknown":
            continue
        references[criterion] = []
        for source_id in finding.evidence_ids:
            # Unique temporary IDs also handle one passage used by two criteria.
            evidence_id = str(len(reviewed))
            reviewed.append(
                facts[source_id].model_copy(
                    update={
                        "id": evidence_id,
                        "field": criterion,
                        "value": finding.status == "supported",
                        "confidence": "supported",
                    }
                )
            )
            references[criterion].append(evidence_id)
    return policy.evaluate(
        Dossier(
            buying_case=dossier.buying_case,
            evidence=reviewed,
            criteria=references,
        ),
        now=now,
    )


@contextmanager
def qualification_scope(stage: str | None, message: str) -> Iterator[None]:
    """Native validation and at most two repairs, within the existing run budget."""
    if stage != "qualify":
        yield
        return
    from robothor.engine.output_validation import output_validation_scope
    from robothor.engine.response_schema import response_schema_scope

    context = json.loads(message)["untrusted_business_data"]
    policy = QualificationPolicy.model_validate(context["policy"])
    dossier = Dossier(
        buying_case=context["buying_case"],
        evidence=[
            Evidence(**e, field="passage", value="unclassified") for e in context["evidence"]
        ],
    )

    def validate(_run: Any, text: str | None) -> str | None:
        try:
            assessment = QualificationAssessment.model_validate_json(text or "")
            evaluate_assessment(policy, dossier, assessment)
        except ValueError:
            return (
                "Return every and only published criterion with status, explanation and unique "
                "existing evidence_ids. Supported/disproved need evidence. Do not return a score."
            )
        return None

    with (
        response_schema_scope(
            "qualification_assessment", QualificationAssessment.model_json_schema()
        ),
        output_validation_scope(validate),
    ):
        yield
