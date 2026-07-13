"""Organization-owned authority boundaries for a self-functioning entity."""

from __future__ import annotations

import uuid
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

from robothor.entity.audit import AuditValue  # noqa: TC001 - Pydantic resolves return schemas
from robothor.entity.payments import Identifier  # noqa: TC001 - Pydantic runtime field
from robothor.entity.policy import DecisionOutcome, PolicyDecision


class AuthorityTier(IntEnum):
    OBSERVE = 0
    REVERSIBLE_OPERATION = 1
    CONTROLLED_COMMITMENT = 2
    PRODUCTION_CHANGE = 3
    AUTHORITY_EXPANSION = 4


class EntityActionKind(StrEnum):
    OBSERVE = "observe"
    LEARNING_UPDATE = "learning_update"
    REVERSIBLE_OPERATION = "reversible_operation"
    EXTERNAL_COMMITMENT = "external_commitment"
    OPERATIONAL_SPEND = "operational_spend"
    PRODUCTION_CHANGE = "production_change"
    SELF_MODIFICATION = "self_modification"
    AUTHORITY_CHANGE = "authority_change"


_ACTION_TIERS: dict[EntityActionKind, AuthorityTier] = {
    EntityActionKind.OBSERVE: AuthorityTier.OBSERVE,
    EntityActionKind.LEARNING_UPDATE: AuthorityTier.REVERSIBLE_OPERATION,
    EntityActionKind.REVERSIBLE_OPERATION: AuthorityTier.REVERSIBLE_OPERATION,
    EntityActionKind.EXTERNAL_COMMITMENT: AuthorityTier.CONTROLLED_COMMITMENT,
    EntityActionKind.OPERATIONAL_SPEND: AuthorityTier.CONTROLLED_COMMITMENT,
    EntityActionKind.PRODUCTION_CHANGE: AuthorityTier.PRODUCTION_CHANGE,
    EntityActionKind.SELF_MODIFICATION: AuthorityTier.PRODUCTION_CHANGE,
    EntityActionKind.AUTHORITY_CHANGE: AuthorityTier.AUTHORITY_EXPANSION,
}


class AuthorityOutcome(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class AuthorityReason(StrEnum):
    WITHIN_DELEGATED_AUTHORITY = "within_delegated_authority"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    OUTSIDE_DELEGATED_TIER = "outside_delegated_tier"
    EXTERNAL_COMMITMENT_REQUIRES_APPROVAL = "external_commitment_requires_approval"
    TREASURY_DECISION_REQUIRED = "treasury_decision_required"
    TREASURY_DECISION_MISMATCH = "treasury_decision_mismatch"
    TREASURY_DENIED = "treasury_denied"
    TREASURY_APPROVAL_REQUIRED = "treasury_approval_required"
    AUTONOMOUS_SPEND_DISABLED = "autonomous_spend_disabled"
    PRODUCTION_CHANGE_REQUIRES_APPROVAL = "production_change_requires_approval"
    SELF_MODIFICATION_REQUIRES_APPROVAL = "self_modification_requires_approval"
    AUTHORITY_CHANGE_REQUIRES_APPROVAL = "authority_change_requires_approval"


class EntityAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    action_id: Identifier
    tenant_id: Identifier
    organization_id: Identifier
    actor_id: Identifier
    kind: EntityActionKind
    purpose: str = Field(min_length=3, max_length=500)
    subject_id: Identifier | None = None

    @property
    def authority_tier(self) -> AuthorityTier:
        return _ACTION_TIERS[self.kind]


class EntityAuthorityPolicy(BaseModel):
    """Delegated autonomy that cannot waive hard human-approval boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: Identifier
    version: int = Field(default=1, ge=1)
    tenant_id: Identifier
    organization_id: Identifier
    max_automatic_tier: AuthorityTier = AuthorityTier.REVERSIBLE_OPERATION
    allow_autonomous_controlled_spend: bool = False
    allow_autonomous_external_commitments: bool = False


class AuthorityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    action_id: Identifier
    authority_tier: AuthorityTier
    policy_id: Identifier
    policy_version: int
    outcome: AuthorityOutcome
    reason: AuthorityReason

    def to_audit_dict(self) -> dict[str, AuditValue]:
        return {
            "decision_id": self.decision_id,
            "action_id": self.action_id,
            "authority_tier": int(self.authority_tier),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "outcome": self.outcome.value,
            "reason": self.reason.value,
        }


class EntityAuthorityEngine:
    """Apply hard boundaries after any domain-specific policy decision."""

    @staticmethod
    def evaluate(
        action: EntityAction,
        *,
        policy: EntityAuthorityPolicy,
        treasury_decision: PolicyDecision | None = None,
    ) -> AuthorityDecision:
        if action.tenant_id != policy.tenant_id or action.organization_id != policy.organization_id:
            return EntityAuthorityEngine._decision(
                action, policy, AuthorityOutcome.DENY, AuthorityReason.OWNERSHIP_MISMATCH
            )

        # These boundaries are intentionally non-configurable.  Staged tests
        # may be automatic, but deployment or expanded authority is not.
        if action.kind is EntityActionKind.PRODUCTION_CHANGE:
            return EntityAuthorityEngine._decision(
                action,
                policy,
                AuthorityOutcome.APPROVAL_REQUIRED,
                AuthorityReason.PRODUCTION_CHANGE_REQUIRES_APPROVAL,
            )
        if action.kind is EntityActionKind.SELF_MODIFICATION:
            return EntityAuthorityEngine._decision(
                action,
                policy,
                AuthorityOutcome.APPROVAL_REQUIRED,
                AuthorityReason.SELF_MODIFICATION_REQUIRES_APPROVAL,
            )
        if action.kind is EntityActionKind.AUTHORITY_CHANGE:
            return EntityAuthorityEngine._decision(
                action,
                policy,
                AuthorityOutcome.APPROVAL_REQUIRED,
                AuthorityReason.AUTHORITY_CHANGE_REQUIRES_APPROVAL,
            )

        if action.kind is EntityActionKind.OPERATIONAL_SPEND:
            if treasury_decision is None:
                return EntityAuthorityEngine._decision(
                    action,
                    policy,
                    AuthorityOutcome.DENY,
                    AuthorityReason.TREASURY_DECISION_REQUIRED,
                )
            if action.subject_id != treasury_decision.proposal_id:
                return EntityAuthorityEngine._decision(
                    action,
                    policy,
                    AuthorityOutcome.DENY,
                    AuthorityReason.TREASURY_DECISION_MISMATCH,
                )
            if treasury_decision.outcome is DecisionOutcome.DENY:
                return EntityAuthorityEngine._decision(
                    action,
                    policy,
                    AuthorityOutcome.DENY,
                    AuthorityReason.TREASURY_DENIED,
                )
            if treasury_decision.outcome is DecisionOutcome.APPROVAL_REQUIRED:
                return EntityAuthorityEngine._decision(
                    action,
                    policy,
                    AuthorityOutcome.APPROVAL_REQUIRED,
                    AuthorityReason.TREASURY_APPROVAL_REQUIRED,
                )
            if not policy.allow_autonomous_controlled_spend:
                return EntityAuthorityEngine._decision(
                    action,
                    policy,
                    AuthorityOutcome.APPROVAL_REQUIRED,
                    AuthorityReason.AUTONOMOUS_SPEND_DISABLED,
                )
            if action.authority_tier > policy.max_automatic_tier:
                return EntityAuthorityEngine._decision(
                    action,
                    policy,
                    AuthorityOutcome.APPROVAL_REQUIRED,
                    AuthorityReason.OUTSIDE_DELEGATED_TIER,
                )
            return EntityAuthorityEngine._decision(
                action,
                policy,
                AuthorityOutcome.ALLOW,
                AuthorityReason.WITHIN_DELEGATED_AUTHORITY,
            )

        if (
            action.kind is EntityActionKind.EXTERNAL_COMMITMENT
            and not policy.allow_autonomous_external_commitments
        ):
            return EntityAuthorityEngine._decision(
                action,
                policy,
                AuthorityOutcome.APPROVAL_REQUIRED,
                AuthorityReason.EXTERNAL_COMMITMENT_REQUIRES_APPROVAL,
            )
        if action.authority_tier > policy.max_automatic_tier:
            return EntityAuthorityEngine._decision(
                action,
                policy,
                AuthorityOutcome.APPROVAL_REQUIRED,
                AuthorityReason.OUTSIDE_DELEGATED_TIER,
            )
        return EntityAuthorityEngine._decision(
            action,
            policy,
            AuthorityOutcome.ALLOW,
            AuthorityReason.WITHIN_DELEGATED_AUTHORITY,
        )

    @staticmethod
    def _decision(
        action: EntityAction,
        policy: EntityAuthorityPolicy,
        outcome: AuthorityOutcome,
        reason: AuthorityReason,
    ) -> AuthorityDecision:
        name = ":".join(
            (action.action_id, policy.policy_id, str(policy.version), outcome.value, reason.value)
        )
        return AuthorityDecision(
            decision_id=str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
            action_id=action.action_id,
            authority_tier=action.authority_tier,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            outcome=outcome,
            reason=reason,
        )


__all__ = [
    "AuthorityDecision",
    "AuthorityOutcome",
    "AuthorityReason",
    "AuthorityTier",
    "EntityAction",
    "EntityActionKind",
    "EntityAuthorityEngine",
    "EntityAuthorityPolicy",
]
