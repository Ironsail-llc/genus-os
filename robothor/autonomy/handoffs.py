"""Durable owner handoffs; acknowledgment can only request reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4  # noqa: TC003 -- Pydantic field type

from pydantic import Field, field_validator, model_validator

from robothor.autonomy.broker import same_page, url_origin
from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.models import StrictModel, WebOperation

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


class Confirmation(StrictModel):
    url: str = Field(min_length=1, max_length=2000)
    selector: str | None = Field(default=None, min_length=1, max_length=500)
    text: str | None = Field(default=None, min_length=3, max_length=300)
    session_resource_id: UUID | None = None

    @model_validator(mode="after")
    def paired_confirmation(self) -> Confirmation:
        if bool(self.selector) != bool(self.text):
            raise ValueError("confirmation_selector_and_text_required_together")
        return self

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url_origin(value)
        return value


class HandoffRequest(StrictModel):
    request_id: UUID
    kind: Literal["sms", "push", "passkey", "biometric", "issuer", "captcha"]
    confirmation: Confirmation
    lifetime_seconds: int = Field(default=900, ge=60, le=86400, strict=True)


#: One owner request buys at most this many browser attempts.
MAX_CHECK_ATTEMPTS = 3


def _public_state(row: dict[str, Any]) -> str:
    """ "We looked and could not tell" is not "we have not looked".

    Before this, an exhausted check returned to ``awaiting_external_action``,
    byte-identical to a handoff nobody had ever checked, and the page redrew
    the same button as though nothing had happened.
    """
    if row["state"] == "resolved":
        return "resolved"
    if row["expires_at"] <= datetime.now(UTC):
        return "expired"
    if (
        row["state"] == "awaiting_external_action"
        and (row.get("check_attempts") or 0) >= MAX_CHECK_ATTEMPTS
    ):
        return "unconfirmed"
    return cast("str", row["state"])


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "operation_id": str(row["operation_id"]),
        "kind": row["kind"],
        "state": _public_state(row),
        "expires_at": row["expires_at"].isoformat(),
        **({"origin": row["origin"], "purpose": row["purpose"]} if "origin" in row else {}),
    }


def release_expired_handoff(
    store: AutonomyStore, cur: Any, scope: Scope, row: dict[str, Any]
) -> bool:
    """Mark an expired handoff expired and hand its operation to the owner.

    An expired handoff used to leave the operation in ``reconciling`` with
    nothing left that could ever move it, so its reservation burned the
    monthly cap for good and ``cancel`` answered ``invalid_transition``.
    The operation is not completed and no money is freed here -- it becomes
    ``awaiting_input``, which is visible, actionable, and still counted until
    the owner says otherwise through ``AutonomyStore.abandon``.
    """
    cur.execute(
        "UPDATE autonomy_handoffs SET state='expired',check_token=NULL,check_lease_until=NULL,"
        "updated_at=now() WHERE tenant_id=%s AND owner_id=%s AND id=%s "
        "AND state IN ('awaiting_external_action','checking') RETURNING operation_id",
        (scope.tenant_id, scope.owner_id, str(row["id"])),
    )
    if not cur.fetchone():
        return False
    operation_id = str(row["operation_id"])
    store._event(cur, scope, operation_id, "external_action_expired")
    cur.execute(
        "SELECT 1 FROM autonomy_handoffs WHERE tenant_id=%s AND owner_id=%s AND operation_id=%s "
        "AND state IN ('awaiting_external_action','checking')",
        (scope.tenant_id, scope.owner_id, operation_id),
    )
    if cur.fetchone():
        return True
    if store._operation(cur, scope, operation_id)["state"] == "reconciling":
        store._finish(
            cur,
            scope,
            operation_id,
            "awaiting_input",
            input_reason="external_verification_expired",
        )
    return True


def observed_submission_pages(operation: dict[str, Any]) -> set[str]:
    """The pages the BROKER landed on, never a page the agent named.

    Two earlier versions were both wrong in the same direction. The first
    accepted each handoff's own ``confirmation.url``, which made the pin
    circular: the agent registered the page it wanted to be judged against
    and then satisfied the pin with its own declaration, completing a
    purchase against "Order confirmed? You have 30 days to return it." on a
    refund-policy article. The second used the bound plan's ``url``, which is
    still the AGENT's value -- ``bind_plan`` writes what the agent declared,
    so that is true about when it was written, not about whose claim it is.

    What counts is ``landed_urls``: the main-frame navigations the broker
    itself observed while submitting. That both closes the hole and restores
    the shape the feature exists for, because a merchant's POST -> redirect
    lands the browser on the confirmation page and the broker sees it.
    """
    landed = (operation.get("execution_plan") or {}).get("landed_urls") or []
    return {item for item in landed if isinstance(item, str)}


class HandoffStore:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    def create(
        self, scope: Scope, operation_id: str, agent_id: str, spec: HandoffRequest
    ) -> dict[str, Any]:
        spec = HandoffRequest.model_validate(spec.model_dump())
        payload = spec.model_dump_json()
        fingerprint = hashlib.sha256(
            json.dumps([operation_id, agent_id, json.loads(payload)], sort_keys=True).encode()
        ).hexdigest()
        with self.store.transaction(scope) as cur:
            self.store._lock(cur, scope)
            op = self.store._operation(cur, scope, operation_id)
            if op["agent_id"] != agent_id:
                raise PermissionError("agent_not_allowed")
            proposal = WebOperation.model_validate(op["proposal"])
            # Settings, then the grant, then everything else -- and all of it
            # BEFORE the idempotent replay is answered. The early return used
            # to sit above this block, so replaying the same request after
            # revocation or after the switch went off returned success and
            # left the handoff armed.
            self.store._check_settings(cur, scope, proposal)
            policy = self.store._live_policy(cur, scope, op)
            if (spec.confirmation.selector or "").strip().lower() in {"body", "html", "*", ":root"}:
                raise PermissionError("use_automatic_or_specific_confirmation")
            if not spec.confirmation.selector and proposal.action in {"purchase", "subscription"}:
                # Automatic outcome detection is the SUBMISSION classifier. It
                # recognizes an affirmative sentence anywhere on the origin,
                # which is not a basis for calling money settled.
                raise PermissionError("specific_confirmation_required_for_payment")
            if op["state"] not in {"submitting", "reconciling"}:
                # A handoff means "a person is finishing something we started".
                # Before begin_submit nothing has been started, and admitting
                # 'reserved' let prepare -> handoff -> reconcile reach completed
                # with no preflight, terms audit or price verification.
                raise PermissionError("operation_not_pending")
            if url_origin(spec.confirmation.url) != proposal.origin:
                raise PermissionError("destination_mismatch")
            if not any(
                same_page(spec.confirmation.url, landed) for landed in observed_submission_pages(op)
            ):
                # The agent does not get to name the page it will be judged
                # against. The only pages on record are the ones the broker's
                # own browser reached while submitting.
                raise PermissionError("confirmation_page_not_observed")
            decision = self.store._budget_decision(
                cur, scope, policy, proposal, agent_id, operation_id
            )
            if decision != "allow":
                raise PermissionError(decision)
            cur.execute(
                "SELECT * FROM autonomy_handoffs WHERE tenant_id=%s AND owner_id=%s AND request_id=%s",
                (scope.tenant_id, scope.owner_id, str(spec.request_id)),
            )
            prior = cur.fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise PermissionError("handoff_request_changed")
                return _public(prior)
            cur.execute(
                "UPDATE autonomy_handoffs SET state='expired',updated_at=now() WHERE tenant_id=%s AND owner_id=%s AND operation_id=%s AND state IN ('awaiting_external_action','checking') AND expires_at<=now()",
                (scope.tenant_id, scope.owner_id, operation_id),
            )
            cur.execute(
                "SELECT 1 FROM autonomy_handoffs WHERE operation_id=%s AND state IN ('awaiting_external_action','checking')",
                (operation_id,),
            )
            if cur.fetchone():
                raise PermissionError("handoff_already_pending")
            record_id = str(uuid4())
            key_id, keys = self.store.resource_keyring()
            encrypted = seal_resource(
                payload, keys, key_id, scope, "handoff:" + operation_id + ":" + record_id
            )
            cur.execute(
                "INSERT INTO autonomy_handoffs(id,tenant_id,owner_id,operation_id,agent_id,request_id,fingerprint,kind,state,encrypted_value,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'awaiting_external_action',%s,now()+make_interval(secs=>%s)) RETURNING *",
                (
                    record_id,
                    scope.tenant_id,
                    scope.owner_id,
                    operation_id,
                    agent_id,
                    str(spec.request_id),
                    fingerprint,
                    spec.kind,
                    encrypted,
                    spec.lifetime_seconds,
                ),
            )
            result = _public(cur.fetchone())
            # A device action might complete a commitment externally. Never
            # restore a submit-capable state, including after expiry or restart.
            # This goes through the journal's transition table rather than a
            # direct UPDATE: the table is what says reserved cannot become
            # reconciling, and a raw UPDATE simply ignored it.
            if op["state"] != "reconciling":
                self.store._finish(cur, scope, operation_id, "reconciling")
            self.store._event(cur, scope, operation_id, "external_action_requested")
            return result

    def list(self, scope: Scope, agent_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.transaction(scope) as cur:
            cur.execute(
                "SELECT h.id,h.operation_id,h.kind,h.state,h.expires_at,h.check_attempts,"
                "o.proposal->>'origin' AS origin,o.proposal->>'purpose' AS purpose "
                "FROM autonomy_handoffs h JOIN autonomy_operations o ON o.id=h.operation_id "
                "AND o.tenant_id=h.tenant_id AND o.owner_id=h.owner_id "
                "WHERE h.tenant_id=%s AND h.owner_id=%s AND (%s IS NULL OR h.agent_id=%s) "
                "ORDER BY (h.state='resolved'),h.created_at DESC LIMIT 100",
                (scope.tenant_id, scope.owner_id, agent_id, agent_id),
            )
            return [_public(row) for row in cur.fetchall()]

    def release_expired(self, scope: Scope, handoff_id: str) -> None:
        """Commit the release in its own transaction, before any refusal.

        ``acknowledge`` raises on an expired handoff, and a raise inside the
        caller's transaction rolls the release back with it -- which is how
        the operation stayed pinned in ``reconciling`` forever.
        """
        with self.store.transaction(scope) as cur:
            self.store._lock(cur, scope)
            cur.execute(
                "SELECT id,operation_id FROM autonomy_handoffs "
                "WHERE tenant_id=%s AND owner_id=%s AND id=%s AND expires_at<=now() "
                "AND state IN ('awaiting_external_action','checking')",
                (scope.tenant_id, scope.owner_id, handoff_id),
            )
            row = cur.fetchone()
            if row:
                release_expired_handoff(self.store, cur, scope, row)

    def acknowledge(self, scope: Scope, handoff_id: str) -> dict[str, Any]:
        self.release_expired(scope, handoff_id)
        with self.store.transaction(scope) as cur:
            self.store._lock(cur, scope)
            cur.execute(
                "SELECT * FROM autonomy_handoffs WHERE tenant_id=%s AND owner_id=%s AND id=%s",
                (scope.tenant_id, scope.owner_id, handoff_id),
            )
            row = cur.fetchone()
            if not row:
                raise PermissionError("handoff_not_found")
            if row["expires_at"] <= datetime.now(UTC):
                raise PermissionError("handoff_expired")
            op = self.store._operation(cur, scope, str(row["operation_id"]))
            if (
                row["state"] not in {"awaiting_external_action", "checking"}
                or op["state"] != "reconciling"
            ):
                raise PermissionError("handoff_not_pending")
            # Arming a check decrypts the private confirmation plan and leads
            # to durable completion, a payment fact and a receipt. Settings
            # first, then the grant -- the same gate as any other advance.
            self.store._check_settings(cur, scope, WebOperation.model_validate(op["proposal"]))
            self.store._live_policy(cur, scope, op)
            spec = HandoffRequest.model_validate_json(
                open_resource(
                    bytes(row["encrypted_value"]),
                    self.store.keys,
                    scope,
                    "handoff:" + str(row["operation_id"]) + ":" + handoff_id,
                )
            )
            cur.execute(
                "UPDATE autonomy_handoffs SET check_attempts=CASE WHEN state='checking' THEN check_attempts ELSE 0 END,check_lease_until=CASE WHEN state='checking' THEN check_lease_until ELSE NULL END,state='checking',updated_at=now() WHERE id=%s",
                (handoff_id,),
            )
            self.store._event(
                cur, scope, str(row["operation_id"]), "external_status_check_requested"
            )
            return {
                "operation_id": str(row["operation_id"]),
                "agent_id": row["agent_id"],
                "confirmation": spec.confirmation.model_dump(),
            }
