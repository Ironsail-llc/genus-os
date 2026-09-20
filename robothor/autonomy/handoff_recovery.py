"""Leased recovery for acknowledged external verification, never submission."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from robothor.autonomy.crypto import open_resource
from robothor.autonomy.handoffs import HandoffRequest
from robothor.autonomy.models import Scope

if TYPE_CHECKING:
    from robothor.autonomy.store import AutonomyStore


class HandoffChecks:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    def candidates(self) -> list[tuple[Scope, str]]:
        """Private service scan; tenant and owner are rebound before every claim."""
        with self.store.transaction() as cur:
            cur.execute(
                "SELECT tenant_id,owner_id,id FROM autonomy_handoffs WHERE state='checking' "
                "AND (check_lease_until IS NULL OR check_lease_until<=now()) "
                "ORDER BY updated_at LIMIT 32"
            )
            return [
                (Scope(tenant_id=row["tenant_id"], owner_id=row["owner_id"]), str(row["id"]))
                for row in cur.fetchall()
            ]

    def claim(self, scope: Scope, handoff_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            # Exhaustion returns to the owner without freeing a reservation or
            # permitting a new submission. Expiry has the same conservative rule.
            cur.execute(
                "UPDATE autonomy_handoffs SET state=CASE WHEN expires_at<=now() THEN 'expired' "
                "ELSE 'awaiting_external_action' END,check_token=NULL,check_lease_until=NULL,updated_at=now() "
                "WHERE tenant_id=%s AND owner_id=%s AND id=%s AND state='checking' "
                "AND (check_lease_until IS NULL OR check_lease_until<=now()) "
                "AND (expires_at<=now() OR check_attempts>=3)",
                (scope.tenant_id, scope.owner_id, handoff_id),
            )
            token = str(uuid4())
            cur.execute(
                "UPDATE autonomy_handoffs h SET check_token=%s,check_lease_until=now()+interval '240 seconds',"
                "check_attempts=check_attempts+1,updated_at=now() "
                "WHERE h.tenant_id=%s AND h.owner_id=%s AND h.id=%s AND h.state='checking' "
                "AND h.expires_at>now() AND h.check_attempts<3 "
                "AND (h.check_lease_until IS NULL OR h.check_lease_until<=now()) "
                "AND EXISTS (SELECT 1 FROM autonomy_operations o WHERE o.id=h.operation_id "
                "AND o.tenant_id=h.tenant_id AND o.owner_id=h.owner_id AND o.state='reconciling') "
                "RETURNING h.operation_id,h.agent_id",
                (token, scope.tenant_id, scope.owner_id, handoff_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            self.store._event(cur, scope, str(row["operation_id"]), "external_status_check_started")
            return {**row, "operation_id": str(row["operation_id"]), "token": token}

    def confirmation(self, scope: Scope, handoff_id: str, token: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
            cur.execute(
                "SELECT operation_id,encrypted_value FROM autonomy_handoffs "
                "WHERE tenant_id=%s AND owner_id=%s AND id=%s AND check_token=%s "
                "AND state='checking' AND check_lease_until>now() AND expires_at>now()",
                (scope.tenant_id, scope.owner_id, handoff_id, token),
            )
            row = cur.fetchone()
            if not row:
                raise PermissionError("handoff_not_claimed")
            spec = HandoffRequest.model_validate_json(
                open_resource(
                    bytes(row["encrypted_value"]),
                    self.store.keys,
                    scope,
                    "handoff:" + str(row["operation_id"]) + ":" + handoff_id,
                )
            )
            return spec.confirmation.model_dump()

    def finish(self, scope: Scope, handoff_id: str, token: str, *, retry: bool = False) -> None:
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            cur.execute(
                "UPDATE autonomy_handoffs SET state=CASE WHEN expires_at<=now() THEN 'expired' "
                "WHEN %s AND check_attempts<3 THEN 'checking' ELSE 'awaiting_external_action' END,"
                "check_token=NULL,check_lease_until=CASE WHEN %s THEN now()+interval '20 seconds' ELSE NULL END,updated_at=now() "
                "WHERE tenant_id=%s AND owner_id=%s AND id=%s AND state='checking' AND check_token=%s "
                "RETURNING operation_id",
                (retry, retry, scope.tenant_id, scope.owner_id, handoff_id, token),
            )
            row = cur.fetchone()
            if row:
                self.store._event(
                    cur,
                    scope,
                    str(row["operation_id"]),
                    "external_status_check_retry" if retry else "external_status_check_finished",
                )
