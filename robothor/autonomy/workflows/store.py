"""Durable workflow ownership, revisions and duplicate-command results."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from psycopg2.extras import Json

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


def fingerprint(value: dict[str, Any]) -> str:
    # Never retain a verification code, even as a guessable digest.
    safe = {key: item for key, item in value.items() if key != "verification_code"}
    return hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class WorkflowStore:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    @staticmethod
    def _get(cur: Any, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        cur.execute(
            "SELECT id::text,operation_id::text,instance_id::text,origin,fingerprint,state,revision "
            "FROM autonomy_workflows WHERE id=%s AND tenant_id=%s AND owner_id=%s AND agent_id=%s FOR UPDATE",
            (workflow_id, scope.tenant_id, scope.owner_id, agent_id),
        )
        row = cur.fetchone()
        if not row:
            raise PermissionError("workflow_not_found")
        return dict(row)

    def get(self, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
            return self._get(cur, scope, agent_id, workflow_id)

    def code_resume(self, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
            row = self._get(cur, scope, agent_id, workflow_id)
            if row["state"] != "open":
                raise PermissionError("workflow_not_open")
            cur.execute(
                "SELECT request FROM autonomy_workflow_commands WHERE workflow_id=%s "
                "AND state='completed' AND result->>'state'='awaiting_input' "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            )
            command = cur.fetchone()
            if not command or command["request"]["revision"] != row["revision"]:
                raise PermissionError("workflow_not_waiting_for_code")
            return {"revision": row["revision"], "advance": command["request"]["kind"] == "advance"}

    def open(
        self,
        scope: Scope,
        agent_id: str,
        operation_id: str,
        instance_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        self.store.check_authority(scope, operation_id, agent_id)
        digest = fingerprint(request)
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            operation = self.store._operation(cur, scope, operation_id)
            if operation["agent_id"] != agent_id or operation["state"] != "reserved":
                raise PermissionError("operation_not_executable")
            if operation["workflow_id"]:
                row = self._get(cur, scope, agent_id, operation["workflow_id"])
                if row["fingerprint"] != digest:
                    raise PermissionError("workflow_open_changed")
                return row
            workflow_id = str(uuid4())
            cur.execute(
                "INSERT INTO autonomy_workflows (id,tenant_id,owner_id,agent_id,operation_id,instance_id,origin,fingerprint) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    workflow_id,
                    scope.tenant_id,
                    scope.owner_id,
                    agent_id,
                    operation_id,
                    instance_id,
                    operation["proposal"]["origin"],
                    digest,
                ),
            )
            cur.execute(
                "UPDATE autonomy_operations SET workflow_id=%s WHERE id=%s",
                (workflow_id, operation_id),
            )
            self.store._event(cur, scope, workflow_id, "workflow_opened")
            return self._get(cur, scope, agent_id, workflow_id)

    def begin(
        self,
        scope: Scope,
        agent_id: str,
        workflow_id: str,
        instance_id: str,
        command_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        safe = {key: value for key, value in request.items() if key != "verification_code"}
        digest = fingerprint(safe)
        with self.store.transaction() as cur:
            row = self._get(cur, scope, agent_id, workflow_id)
            cur.execute(
                "SELECT fingerprint,state,result FROM autonomy_workflow_commands WHERE workflow_id=%s AND command_id=%s",
                (workflow_id, command_id),
            )
            prior = cur.fetchone()
            if prior:
                if prior["fingerprint"] != digest:
                    raise PermissionError("command_changed")
                return (
                    dict(prior["result"])
                    if prior["state"] == "completed"
                    else {"state": "reconciling", "reason": "command_in_progress"}
                )
            if row["instance_id"] != instance_id:
                raise PermissionError("workflow_lost")
            if row["state"] != "open":
                raise PermissionError("workflow_not_open")
            if request.get("revision") != row["revision"]:
                raise PermissionError("workflow_revision_changed")
            cur.execute(
                "SELECT 1 FROM autonomy_workflow_commands WHERE workflow_id=%s AND state='running'",
                (workflow_id,),
            )
            if cur.fetchone():
                raise PermissionError("command_in_progress")
            cur.execute(
                "INSERT INTO autonomy_workflow_commands (workflow_id,command_id,tenant_id,owner_id,fingerprint,request,state) "
                "VALUES (%s,%s,%s,%s,%s,%s,'running')",
                (workflow_id, command_id, scope.tenant_id, scope.owner_id, digest, Json(safe)),
            )
        return None

    def finish(
        self,
        scope: Scope,
        agent_id: str,
        workflow_id: str,
        command_id: str,
        result: dict[str, Any],
        *,
        advance: bool = False,
    ) -> None:
        with self.store.transaction() as cur:
            self._get(cur, scope, agent_id, workflow_id)
            cur.execute(
                "UPDATE autonomy_workflow_commands SET state='completed',result=%s WHERE workflow_id=%s AND command_id=%s AND state='running'",
                (Json(result), workflow_id, command_id),
            )
            if cur.rowcount != 1:
                raise PermissionError("command_not_running")
            cur.execute(
                "UPDATE autonomy_workflows SET revision=revision+%s,updated_at=now() WHERE id=%s",
                (int(advance), workflow_id),
            )
            self.store._event(cur, scope, workflow_id, "workflow_command_completed")

    def checkpoint(self, scope: Scope, agent_id: str, workflow_id: str) -> None:
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            row = self._get(cur, scope, agent_id, workflow_id)
            operation = self.store._operation(cur, scope, row["operation_id"])
            proposal = operation["proposal"]
            if (
                row["state"] != "open"
                or operation["state"] != "submitting"
                or proposal["action"] not in {"account", "login", "application"}
                or any(
                    proposal.get(key, 0)
                    for key in ("amount_minor", "recurring_minor", "annual_commitment_minor")
                )
            ):
                raise PermissionError("intermediate_step_not_allowed")
            cur.execute(
                "UPDATE autonomy_operations SET state='reserved',execution_plan=NULL,updated_at=now() WHERE id=%s",
                (row["operation_id"],),
            )
            self.store._event(cur, scope, row["operation_id"], "workflow_step_confirmed")

    def close(self, scope: Scope, agent_id: str, workflow_id: str, state: str) -> None:
        if state not in {"completed", "closed", "lost"}:
            raise ValueError("invalid_workflow_state")
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            row = self._get(cur, scope, agent_id, workflow_id)
            if row["state"] != "open":
                return
            if state == "completed":
                operation = self.store._operation(cur, scope, row["operation_id"])
                if operation["state"] != "completed":
                    raise PermissionError("operation_not_completed")
            cur.execute(
                "UPDATE autonomy_workflows SET state=%s,updated_at=now() WHERE id=%s",
                (state, workflow_id),
            )
            if state == "lost":
                cur.execute(
                    "UPDATE autonomy_operations SET state='reconciling',updated_at=now() WHERE id=%s AND state IN ('reserved','submitting','awaiting_input')",
                    (row["operation_id"],),
                )
            self.store._event(cur, scope, workflow_id, "workflow_" + state)
