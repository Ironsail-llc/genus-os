"""Read-only prerequisite preview; execution always rechecks current authority."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 -- Pydantic resolves this at runtime

from pydantic import Field

from robothor.autonomy.models import Kind, StrictModel, WebOperation

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


class ResourceRequirement(StrictModel):
    resource_id: UUID
    kind: Kind
    fields: list[str] = Field(min_length=1, max_length=80)


class ReadinessRequest(StrictModel):
    grant_id: UUID
    proposal: WebOperation
    requirements: list[ResourceRequirement] = Field(default_factory=list, max_length=80)


_REMEDIES = {
    "autonomous_execution_not_enabled": "Enable delegated execution on Personal automation.",
    "payment_processing_not_enabled": "Complete payment setup and its deployment assessment before enabling payments.",
    "operation_already_prepared": "Inspect the existing operation and reconcile uncertainty; do not create another commitment.",
    "idempotency_conflict": "The request identity already belongs to different arguments; inspect the originating task before proceeding.",
    "grant_revoked": "Select an active standing grant or update delegation on Personal automation.",
    "resource_requirements_missing": "Enroll the missing fields securely or select a valid resource for this destination.",
    "monthly_limit": "Wait for available budget or have the owner change the standing spending limit.",
    "monthly_commitment_limit": "Resolve overlapping recurring commitments or have the owner change the monthly limit.",
}


def task_readiness(
    store: AutonomyStore, scope: Scope, agent_id: str, request: ReadinessRequest
) -> dict[str, Any]:
    """Use only scoped metadata; no reservation, decryption or merchant request."""
    blockers: list[str] = []
    settings = store.settings(scope)
    if not settings.enabled:
        blockers.append("autonomous_execution_not_enabled")
    if request.proposal.action in {"purchase", "subscription"} and not settings.payment_processing:
        blockers.append("payment_processing_not_enabled")
    existing_operation = None
    with store.transaction(scope) as cur:
        cur.execute(
            "SELECT id::text,state,fingerprint FROM autonomy_operations "
            "WHERE tenant_id=%s AND owner_id=%s AND idempotency_key=%s",
            (scope.tenant_id, scope.owner_id, request.proposal.idempotency_key),
        )
        existing = cur.fetchone()
        if existing:
            payload = request.proposal.model_dump(mode="json", exclude_none=True)
            fingerprint = hashlib.sha256(
                json.dumps([str(request.grant_id), agent_id, payload], sort_keys=True).encode()
            ).hexdigest()
            if existing["fingerprint"] == fingerprint:
                existing_operation = {"id": existing["id"], "state": existing["state"]}
                blockers.append("operation_already_prepared")
            else:
                blockers.append("idempotency_conflict")
        try:
            policy, _ = store._policy(cur, scope, str(request.grant_id))
            decision = (
                policy.decision(request.proposal, agent_id=agent_id, used_minor=0)
                if existing
                else store._budget_decision(cur, scope, policy, request.proposal, agent_id)
            )
            if decision != "allow":
                blockers.append(decision)
        except PermissionError:
            blockers.append("grant_revoked")
    available = {row["id"]: row for row in store.resources(scope)}
    requirements = []
    for index, required in enumerate(request.requirements):
        row = available.get(str(required.resource_id))
        usable = bool(
            row
            and row["kind"] == required.kind
            and (not row["origin"] or row["origin"] == request.proposal.origin)
        )
        fields = set(row["descriptor"].get("fields", [])) if usable and row else set()
        # Return caller requirement indices, never foreign references or labels.
        missing = sorted(set(required.fields) - fields)
        requirements.append(
            {
                "index": index,
                "status": "available"
                if usable and not missing
                else "missing_fields"
                if usable
                else "unavailable",
                "missing_fields": missing,
            }
        )
    if any(row["status"] != "available" for row in requirements):
        blockers.append("resource_requirements_missing")
    return {
        "ready_to_prepare": not blockers,
        "existing_operation": existing_operation,
        "blockers": blockers,
        "remedies": [
            {
                "reason": reason,
                "action": _REMEDIES.get(
                    reason, "Review the proposal against the current standing grant."
                ),
            }
            for reason in blockers
        ],
        "requirements": requirements,
        "unchecked": [
            "resource_decryption",
            "browser_execution",
            "merchant_requirements",
            "funding_acceptance",
            "verification_challenges",
        ],
        "setup_path": "/account/autonomy",
        "reservation_created": False,
    }
