"""Leased recovery for acknowledged external verification, never submission."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from robothor.autonomy.crypto import open_resource
from robothor.autonomy.handoffs import HandoffRequest, release_expired_handoff
from robothor.autonomy.models import Scope, WebOperation

if TYPE_CHECKING:
    from robothor.autonomy.store import AutonomyStore


class HandoffChecks:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    #: One pass visits at most this many owners, oldest work first.
    SCAN_LIMIT = 32

    def enabled_scopes(self) -> list[Scope]:
        """Owners who have the switch ON *and* have work waiting.

        Two earlier shapes were both wrong. The original scanned every tenant
        in the database with no binding at all. Replacing it with a full
        ``SELECT ... FROM autonomy_settings`` fixed the disabled owner but not
        the process: no WHERE, no LIMIT, no sharding, in every bridge worker
        every 60 seconds. This asks the opposite question -- which owners have
        a check to run or a lapsed handoff to release -- so an idle or
        disabled deployment returns nothing, and a busy one is bounded and
        rotates oldest-first as work is consumed.
        """
        with self.store.transaction() as cur:
            cur.execute(
                "SELECT h.tenant_id,h.owner_id FROM autonomy_handoffs h "
                "JOIN autonomy_settings s ON s.tenant_id=h.tenant_id AND s.owner_id=h.owner_id "
                "WHERE COALESCE((s.settings->>'enabled')::boolean,false) AND ("
                "  (h.state='checking'"
                "   AND (h.check_lease_until IS NULL OR h.check_lease_until<=now()))"
                "  OR (h.state IN ('awaiting_external_action','checking') AND h.expires_at<=now())"
                ") GROUP BY h.tenant_id,h.owner_id ORDER BY min(h.updated_at) LIMIT %s",
                (self.SCAN_LIMIT,),
            )
            return [
                Scope(tenant_id=row["tenant_id"], owner_id=row["owner_id"])
                for row in cur.fetchall()
            ]

    def candidates(self, scope: Scope) -> list[str]:
        """Bound to one tenant and owner. This scanned every tenant."""
        with self.store.transaction() as cur:
            cur.execute(
                "SELECT id FROM autonomy_handoffs WHERE tenant_id=%s AND owner_id=%s "
                "AND state='checking' "
                "AND (check_lease_until IS NULL OR check_lease_until<=now()) "
                "ORDER BY updated_at LIMIT 32",
                (scope.tenant_id, scope.owner_id),
            )
            return [str(row["id"]) for row in cur.fetchall()]

    def release_expired(self, scope: Scope) -> int:
        """Hand every lapsed handoff's operation back to the owner."""
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            cur.execute(
                "SELECT id,operation_id FROM autonomy_handoffs "
                "WHERE tenant_id=%s AND owner_id=%s AND expires_at<=now() "
                "AND state IN ('awaiting_external_action','checking')",
                (scope.tenant_id, scope.owner_id),
            )
            rows = list(cur.fetchall())
            return sum(release_expired_handoff(self.store, cur, scope, row) for row in rows)

    def claim(self, scope: Scope, handoff_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            # Exhaustion returns to the owner without freeing a reservation or
            # permitting a new submission. Expiry releases the operation.
            cur.execute(
                "SELECT id,operation_id,expires_at,state,check_attempts FROM autonomy_handoffs "
                "WHERE tenant_id=%s AND owner_id=%s AND id=%s",
                (scope.tenant_id, scope.owner_id, handoff_id),
            )
            record = cur.fetchone()
            if record and record["state"] in {"awaiting_external_action", "checking"}:
                if record["expires_at"] <= _now(cur):
                    release_expired_handoff(self.store, cur, scope, record)
                    return None
                if record["state"] == "checking" and record["check_attempts"] >= 3:
                    cur.execute(
                        "UPDATE autonomy_handoffs SET state='awaiting_external_action',"
                        "check_token=NULL,check_lease_until=NULL,updated_at=now() "
                        "WHERE id=%s AND state='checking' "
                        "AND (check_lease_until IS NULL OR check_lease_until<=now())",
                        (handoff_id,),
                    )
            if record and not self._still_authorized(cur, scope, record):
                # A revoked grant or a disabled feature EXPIRES the handoff
                # rather than checking it: the probe that restored a saved
                # session and reached completed started exactly here.
                release_expired_handoff(self.store, cur, scope, record)
                return None
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

    def _still_authorized(self, cur: Any, scope: Scope, record: dict[str, Any]) -> bool:
        """Settings first, then the grant -- re-read, not remembered.

        A restart is not a grant. The daemon used to check only state, expiry
        and attempt count, so a service that came back up after the owner
        revoked and switched off carried on where it left off.
        """
        try:
            operation = self.store._operation(cur, scope, str(record["operation_id"]))
        except PermissionError:
            return False
        try:
            self.store._check_settings(
                cur, scope, WebOperation.model_validate(operation["proposal"])
            )
            self.store._live_policy(cur, scope, operation)
        except PermissionError:
            return False
        return True

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


def _now(cur: Any) -> Any:
    cur.execute("SELECT now() AS now")
    return cur.fetchone()["now"]
