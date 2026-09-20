"""Durable owner handoffs; acknowledgment can only request reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4  # noqa: TC003 -- Pydantic field type

from pydantic import Field, field_validator

from robothor.autonomy.broker import url_origin
from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.models import StrictModel, WebOperation

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


class Confirmation(StrictModel):
    url: str = Field(min_length=1, max_length=2000)
    selector: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=3, max_length=300)
    session_resource_id: UUID | None = None

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


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "operation_id": str(row["operation_id"]),
        "kind": row["kind"],
        "state": "expired"
        if row["state"] != "resolved" and row["expires_at"] <= datetime.now(UTC)
        else row["state"],
        "expires_at": row["expires_at"].isoformat(),
        **({"origin": row["origin"], "purpose": row["purpose"]} if "origin" in row else {}),
    }


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
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            op = self.store._operation(cur, scope, operation_id)
            if op["agent_id"] != agent_id:
                raise PermissionError("agent_not_allowed")
            cur.execute(
                "SELECT * FROM autonomy_handoffs WHERE tenant_id=%s AND owner_id=%s AND request_id=%s",
                (scope.tenant_id, scope.owner_id, str(spec.request_id)),
            )
            prior = cur.fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise PermissionError("handoff_request_changed")
                return _public(prior)
            if op["state"] not in {"reserved", "submitting", "reconciling"}:
                raise PermissionError("operation_not_pending")
            if url_origin(spec.confirmation.url) != op["proposal"]["origin"]:
                raise PermissionError("destination_mismatch")
            self.store._check_settings(cur, scope, WebOperation.model_validate(op["proposal"]))
            policy, version = self.store._policy(cur, scope, op["grant_id"])
            if version != op["grant_version"]:
                raise PermissionError("grant_changed")
            decision = self.store._budget_decision(
                cur,
                scope,
                policy,
                WebOperation.model_validate(op["proposal"]),
                agent_id,
                operation_id,
            )
            if decision != "allow":
                raise PermissionError(decision)
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
            cur.execute(
                "UPDATE autonomy_operations SET state='reconciling',updated_at=now() WHERE id=%s",
                (operation_id,),
            )
            self.store._event(cur, scope, operation_id, "external_action_requested")
            return result

    def list(self, scope: Scope, agent_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.transaction() as cur:
            cur.execute(
                "SELECT h.id,h.operation_id,h.kind,h.state,h.expires_at,"
                "o.proposal->>'origin' AS origin,o.proposal->>'purpose' AS purpose "
                "FROM autonomy_handoffs h JOIN autonomy_operations o ON o.id=h.operation_id "
                "AND o.tenant_id=h.tenant_id AND o.owner_id=h.owner_id "
                "WHERE h.tenant_id=%s AND h.owner_id=%s AND (%s IS NULL OR h.agent_id=%s) "
                "ORDER BY (h.state='resolved'),h.created_at DESC LIMIT 100",
                (scope.tenant_id, scope.owner_id, agent_id, agent_id),
            )
            return [_public(row) for row in cur.fetchall()]

    def acknowledge(self, scope: Scope, handoff_id: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
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
            spec = HandoffRequest.model_validate_json(
                open_resource(
                    bytes(row["encrypted_value"]),
                    self.store.keys,
                    scope,
                    "handoff:" + str(row["operation_id"]) + ":" + handoff_id,
                )
            )
            cur.execute(
                "UPDATE autonomy_handoffs SET state='checking',updated_at=now() WHERE id=%s",
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
