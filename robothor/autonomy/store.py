"""Native-vault resources and transactional delegated-operation journal.

No network mutation occurs inside a database transaction. Reserve before
execution, mark submitting before the first external commitment, then reconcile
uncertain outcomes. A submitting operation is never automatically retried.
"""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor

from robothor.autonomy.budget import monthly_projection, proposal_record
from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.descriptors import Source, describe, refresh_descriptors
from robothor.autonomy.models import (
    Delegation,
    PaymentCard,
    RequestContext,
    ResourceInput,
    RuntimeSettings,
    Scope,
    WebOperation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from robothor.autonomy.procedures import ProcedureQuery


def _validated_payload(resource: ResourceInput) -> str:
    """Validate before encryption without ever echoing an invalid input."""
    try:
        value = json.loads(resource.payload.get_secret_value())
        if not isinstance(value, dict):
            raise ValueError
        if resource.kind in {"credential", "totp", "browser_session"} and not resource.origin:
            raise ValueError
        if resource.kind == "payment_card":
            card = PaymentCard.model_validate(value)
            value = {
                "number": card.number.get_secret_value(),
                "name": card.name.get_secret_value(),
                "expiry_month": card.expiry_month,
                "expiry_year": card.expiry_year,
            }
        elif resource.kind == "credential":
            if set(value) != {"username", "password"} or not all(
                isinstance(v, str) and 0 < len(v) <= 4096 for v in value.values()
            ):
                raise ValueError
        elif resource.kind == "totp":
            if set(value) != {"secret"}:
                raise ValueError
            base64.b32decode(value["secret"].upper(), casefold=True)
        elif resource.kind == "document":
            if set(value) != {"name", "mime_type", "base64"}:
                raise ValueError
            if not isinstance(value["name"], str) or any(c in value["name"] for c in "/\\\0"):
                raise ValueError
            if len(base64.b64decode(value["base64"], validate=True)) > 5_000_000:
                raise ValueError
        elif resource.kind == "profile":
            import re

            answers = value.pop("answers", {})
            if (
                not isinstance(answers, dict)
                or len(answers) > 80
                or any(
                    not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,59}", key)
                    or not isinstance(answer, str)
                    or len(answer) > 5000
                    for key, answer in answers.items()
                )
            ):
                raise ValueError
            allowed = {
                "first_name",
                "last_name",
                "legal_name",
                "email",
                "phone",
                "date_of_birth",
                "address_line1",
                "address_line2",
                "city",
                "region",
                "postal_code",
                "country",
                "occupation",
                "employer",
                "interests",
                "nationality",
            }
            if not set(value) <= allowed or not all(isinstance(v, str) for v in value.values()):
                raise ValueError
            if answers:
                value["answers"] = answers
        elif resource.kind == "browser_session":
            if not set(value) <= {"cookies", "origins"}:
                raise ValueError
        return json.dumps(value, separators=(",", ":"))
    except Exception:
        raise ValueError("invalid_resource_payload") from None


class AutonomyStore:
    def __init__(
        self,
        connect: Callable[[], Any] | None = None,
        *,
        keys: dict[str, bytes] | None = None,
        key_id: str | None = None,
    ) -> None:
        self._connect = connect
        self._keys = keys
        self._key_id = key_id

    def resource_keyring(self) -> tuple[str, dict[str, bytes]]:
        if self._keys is not None:
            return self._key_id or "native-v1", self._keys
        from robothor.vault.crypto import decrypt, get_master_key

        master = get_master_key()
        keys = {"native-v1": master}
        active = "native-v1"
        with self.transaction() as cur:
            cur.execute("SELECT id,encrypted_key,active FROM autonomy_key_versions")
            for row in cur.fetchall():
                keys[row["id"]] = base64.b64decode(decrypt(bytes(row["encrypted_key"]), master))
                if row["active"]:
                    active = row["id"]
        return active, keys

    @property
    def key_id(self) -> str:
        return self.resource_keyring()[0]

    @property
    def keys(self) -> dict[str, bytes]:
        return self.resource_keyring()[1]

    def rotate_resource_keyring(self) -> int:
        """Administrative rotation; old keys remain for in-flight writes and recovery."""
        import secrets

        from robothor.vault.crypto import encrypt, get_master_key

        master = get_master_key()
        with self.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('autonomy-key-rotation',0))")
            _, keys = self.resource_keyring()
            key_id = "resource-" + uuid4().hex
            keys[key_id] = secrets.token_bytes(32)
            encrypted_key = encrypt(base64.b64encode(keys[key_id]).decode(), master)
            cur.execute(
                "SELECT id::text,tenant_id,owner_id,encrypted_value FROM vault_resources FOR UPDATE"
            )
            rows = cur.fetchall()
            for row in rows:
                scope = Scope(tenant_id=row["tenant_id"], owner_id=row["owner_id"])
                value = open_resource(bytes(row["encrypted_value"]), keys, scope, row["id"])
                cur.execute(
                    "UPDATE vault_resources SET encrypted_value=%s,updated_at=now() WHERE id=%s",
                    (seal_resource(value, keys, key_id, scope, row["id"]), row["id"]),
                )
                self._event(cur, scope, row["id"], "resource_rotated")
            cur.execute("UPDATE autonomy_key_versions SET active=false WHERE active")
            cur.execute(
                "INSERT INTO autonomy_key_versions(id,encrypted_key,active) VALUES (%s,%s,true)",
                (key_id, encrypted_key),
            )
            return len(rows)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        if self._connect:
            conn = self._connect()
        else:
            import psycopg2

            from robothor.config import get_config
            from robothor.db.connection import assert_test_database

            config = get_config().db
            assert_test_database(config.name)
            conn = psycopg2.connect(**config.dict, connect_timeout=5)
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    yield cur
        finally:
            conn.close()

    @staticmethod
    def _lock(cur: Any, scope: Scope) -> None:
        # All grants for the same owner share one reservation lock. Locking
        # only grant_id would let parallel grants spend the same budget twice.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (json.dumps([scope.tenant_id, scope.owner_id]),),
        )

    @staticmethod
    def _event(cur: Any, scope: Scope, subject_id: str, event: str) -> None:
        cur.execute(
            "INSERT INTO autonomy_events (tenant_id,owner_id,subject_id,event) "
            "VALUES (%s,%s,%s,%s)",
            (scope.tenant_id, scope.owner_id, subject_id, event),
        )

    def put_resource(
        self,
        scope: Scope,
        resource: ResourceInput,
        *,
        lifetime_seconds: int | None = None,
        source: Source = "secure_input",
    ) -> dict[str, Any]:
        if lifetime_seconds is not None and not 1 <= lifetime_seconds <= 600:
            raise ValueError("invalid_verification_lifetime")
        from robothor.entity.audit import redact_for_audit
        from robothor.secrets.redaction import redact

        if (
            redact_for_audit(resource.label) != resource.label
            or redact(resource.label) != resource.label
        ):
            raise ValueError("resource_label_contains_secret")
        value = _validated_payload(resource)
        descriptor = describe(resource.kind, value, source)
        resource_id = str(uuid4())
        key_id, keys = self.resource_keyring()
        sealed = seal_resource(value, keys, key_id, scope, resource_id)
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO vault_resources "
                "(id,tenant_id,owner_id,kind,label,origin,encrypted_value,expires_at,descriptor) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,now() + %s::integer * interval '1 second',%s)",
                (
                    resource_id,
                    scope.tenant_id,
                    scope.owner_id,
                    resource.kind,
                    resource.label,
                    resource.origin,
                    sealed,
                    lifetime_seconds,
                    Json(descriptor),
                ),
            )
            self._event(cur, scope, resource_id, "resource_created")
        return {
            "id": resource_id,
            "kind": resource.kind,
            "label": resource.label,
            "origin": resource.origin,
            "descriptor": descriptor,
        }

    def resources(self, scope: Scope) -> list[dict[str, Any]]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT id::text,kind,label,origin,descriptor FROM vault_resources "
                "WHERE tenant_id=%s AND owner_id=%s AND active AND (expires_at IS NULL OR expires_at>now()) ORDER BY created_at",
                (scope.tenant_id, scope.owner_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def refresh_resource_descriptors(self, scope: Scope) -> int:
        return refresh_descriptors(self, scope)

    def consume_resource(
        self, scope: Scope, resource_id: str, destination: str, *, kind: str | None = None
    ) -> dict[str, Any]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT kind,origin,encrypted_value FROM vault_resources "
                "WHERE id=%s AND tenant_id=%s AND owner_id=%s AND active AND (expires_at IS NULL OR expires_at>now())",
                (resource_id, scope.tenant_id, scope.owner_id),
            )
            row = cur.fetchone()
            if (
                not row
                or (kind and row["kind"] != kind)
                or (row["origin"] and row["origin"] != destination)
            ):
                raise PermissionError("resource_not_authorized")
            plaintext = open_resource(bytes(row["encrypted_value"]), self.keys, scope, resource_id)
            self._event(cur, scope, resource_id, "resource_consumed")
            return cast("dict[str, Any]", json.loads(plaintext))

    def revoke_resource(self, scope: Scope, resource_id: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE vault_resources SET active=false,updated_at=now() "
                "WHERE id=%s AND tenant_id=%s AND owner_id=%s RETURNING id",
                (resource_id, scope.tenant_id, scope.owner_id),
            )
            if not cur.fetchone():
                raise PermissionError("resource_not_found")
            self._event(cur, scope, resource_id, "resource_revoked")

    def create_grant(self, scope: Scope, policy: Delegation) -> dict[str, Any]:
        grant_id = str(uuid4())
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO autonomy_grants(id,tenant_id,owner_id,policy) VALUES (%s,%s,%s,%s)",
                (grant_id, scope.tenant_id, scope.owner_id, Json(policy.model_dump(mode="json"))),
            )
            self._event(cur, scope, grant_id, "grant_created")
        return dict(id=grant_id, version=1, **policy.model_dump(mode="json"))

    def grants(self, scope: Scope) -> list[dict[str, Any]]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT id::text,version,policy,revoked_at IS NOT NULL AS revoked "
                "FROM autonomy_grants WHERE tenant_id=%s AND owner_id=%s",
                (scope.tenant_id, scope.owner_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def revoke_grant(self, scope: Scope, grant_id: str) -> None:
        with self.transaction() as cur:
            self._lock(cur, scope)
            cur.execute(
                "UPDATE autonomy_grants SET revoked_at=now(),version=version+1 "
                "WHERE id=%s AND tenant_id=%s AND owner_id=%s RETURNING id",
                (grant_id, scope.tenant_id, scope.owner_id),
            )
            if not cur.fetchone():
                raise PermissionError("grant_not_found")
            self._event(cur, scope, grant_id, "grant_revoked")

    def _policy(self, cur: Any, scope: Scope, grant_id: str) -> tuple[Delegation, int]:
        cur.execute(
            "SELECT policy,version,revoked_at FROM autonomy_grants "
            "WHERE id=%s AND tenant_id=%s AND owner_id=%s",
            (grant_id, scope.tenant_id, scope.owner_id),
        )
        row = cur.fetchone()
        if not row or row["revoked_at"]:
            raise PermissionError("grant_revoked")
        return Delegation.model_validate(row["policy"]), row["version"]

    @staticmethod
    def _budget_decision(
        cur: Any,
        scope: Scope,
        policy: Delegation,
        proposal: WebOperation,
        agent_id: str,
        exclude: str | None = None,
    ) -> str:
        if proposal.action not in {"purchase", "subscription"}:
            return policy.decision(proposal, agent_id=agent_id, used_minor=0)
        if proposal.recurring_minor and not proposal.recurrence:
            return "renewal_schedule_missing"
        now = datetime.now(UTC)
        if (
            not exclude
            and proposal.recurrence
            and not (
                now.date() <= proposal.recurrence.next_charge_on <= now.date() + timedelta(days=366)
            )
        ):
            return "renewal_date_out_of_range"
        cur.execute(
            "SELECT proposal,state,updated_at FROM autonomy_operations "
            "WHERE tenant_id=%s AND owner_id=%s AND proposal->>'currency'=%s "
            "AND (%s IS NULL OR id::text<>%s) "
            "AND state IN ('reserved','submitting','reconciling','awaiting_input','completed')",
            (scope.tenant_id, scope.owner_id, proposal.currency, exclude, exclude),
        )
        try:
            before = monthly_projection(list(cur.fetchall()), today=now.date())
            added = monthly_projection(
                [proposal_record(proposal.model_dump(mode="json"), now=now)], today=now.date()
            )
        except ValueError:
            return "renewal_schedule_missing"
        month = now.strftime("%Y-%m")
        decision = policy.decision(
            proposal,
            agent_id=agent_id,
            used_minor=before[month] + added[month] - proposal.amount_minor,
        )
        if decision != "allow":
            return decision
        if proposal.recurring_minor and any(
            before[key] + amount > policy.monthly_minor for key, amount in added.items()
        ):
            return "monthly_commitment_limit"
        return "allow"

    def reserve(
        self,
        scope: Scope,
        grant_id: str,
        agent_id: str,
        proposal: WebOperation,
        *,
        request_context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = proposal.model_dump(mode="json", exclude_none=True)
        fingerprint = hashlib.sha256(
            json.dumps([grant_id, agent_id, payload], sort_keys=True).encode()
        ).hexdigest()
        with self.transaction() as cur:
            self._lock(cur, scope)
            cur.execute(
                "SELECT id::text,state,fingerprint FROM autonomy_operations "
                "WHERE tenant_id=%s AND owner_id=%s AND idempotency_key=%s",
                (scope.tenant_id, scope.owner_id, proposal.idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise PermissionError("idempotency_conflict")
                return {"id": existing["id"], "state": existing["state"]}
            policy, version = self._policy(cur, scope, grant_id)
            decision = self._budget_decision(cur, scope, policy, proposal, agent_id)
            if decision != "allow":
                raise PermissionError(decision)
            operation_id = str(uuid4())
            cur.execute(
                "INSERT INTO autonomy_operations "
                "(id,tenant_id,owner_id,grant_id,grant_version,agent_id,idempotency_key,"
                "fingerprint,proposal,request_context,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved')",
                (
                    operation_id,
                    scope.tenant_id,
                    scope.owner_id,
                    grant_id,
                    version,
                    agent_id,
                    proposal.idempotency_key,
                    fingerprint,
                    Json(payload),
                    Json(request_context.model_dump(mode="json")) if request_context else None,
                ),
            )
            self._event(cur, scope, operation_id, "reserved")
            return {"id": operation_id, "state": "reserved"}

    def operation(self, scope: Scope, operation_id: str) -> dict[str, Any]:
        with self.transaction() as cur:
            return self._operation(cur, scope, operation_id)

    def procedures(
        self, scope: Scope, agent_id: str, query: ProcedureQuery
    ) -> list[dict[str, Any]]:
        from robothor.autonomy.procedures import find_procedures

        return find_procedures(self, scope, agent_id, query)

    @staticmethod
    def _operation(cur: Any, scope: Scope, operation_id: str) -> dict[str, Any]:
        cur.execute(
            "SELECT id::text,grant_id::text,grant_version,agent_id,proposal,request_context,state,evidence,execution_plan,input_reason,workflow_id::text,extract(epoch FROM created_at)::bigint AS created_epoch "
            "FROM autonomy_operations WHERE id=%s AND tenant_id=%s AND owner_id=%s",
            (operation_id, scope.tenant_id, scope.owner_id),
        )
        row = cur.fetchone()
        if not row:
            raise PermissionError("operation_not_found")
        return dict(row)

    def begin_submit(
        self, scope: Scope, operation_id: str, agent_id: str, *, workflow_id: str | None = None
    ) -> None:
        with self.transaction() as cur:
            self._lock(cur, scope)
            row = self._operation(cur, scope, operation_id)
            if row.get("workflow_id") != workflow_id:
                raise PermissionError("workflow_required")
            if row["state"] != "reserved":
                raise PermissionError("reconciliation_required")
            if row["agent_id"] != agent_id:
                raise PermissionError("agent_not_allowed")
            policy, version = self._policy(cur, scope, row["grant_id"])
            if version != row["grant_version"]:
                raise PermissionError("grant_changed")
            proposal = WebOperation.model_validate(row["proposal"])
            self._check_settings(cur, scope, proposal)
            decision = self._budget_decision(cur, scope, policy, proposal, agent_id, operation_id)
            if decision != "allow":
                raise PermissionError(decision)
            cur.execute(
                "UPDATE autonomy_operations SET state='submitting',updated_at=now() WHERE id=%s",
                (operation_id,),
            )
            self._event(cur, scope, operation_id, "submitting")

    def check_authority(self, scope: Scope, operation_id: str, agent_id: str) -> Delegation:
        with self.transaction() as cur:
            self._lock(cur, scope)
            row = self._operation(cur, scope, operation_id)
            if row["agent_id"] != agent_id or row["state"] not in {"reserved", "submitting"}:
                raise PermissionError("operation_not_executable")
            policy, version = self._policy(cur, scope, row["grant_id"])
            if version != row["grant_version"]:
                raise PermissionError("grant_changed")
            proposal = WebOperation.model_validate(row["proposal"])
            self._check_settings(cur, scope, proposal)
            result = self._budget_decision(cur, scope, policy, proposal, agent_id, operation_id)
            if result != "allow":
                raise PermissionError(result)
            return policy

    @staticmethod
    def _check_settings(cur: Any, scope: Scope, proposal: WebOperation) -> None:
        cur.execute(
            "SELECT settings FROM autonomy_settings WHERE tenant_id=%s AND owner_id=%s",
            (scope.tenant_id, scope.owner_id),
        )
        row = cur.fetchone()
        settings = RuntimeSettings.model_validate(row["settings"]) if row else RuntimeSettings()
        if not settings.enabled:
            raise PermissionError("autonomous_execution_not_enabled")
        if proposal.action in {"purchase", "subscription"} and not settings.payment_processing:
            raise PermissionError("payment_processing_not_enabled")

    def bind_plan(
        self, scope: Scope, operation_id: str, agent_id: str, plan: dict[str, Any]
    ) -> None:
        with self.transaction() as cur:
            self._lock(cur, scope)
            row = self._operation(cur, scope, operation_id)
            if row["agent_id"] != agent_id or row["state"] != "reserved":
                raise PermissionError("operation_not_executable")
            if row["execution_plan"] is not None and row["execution_plan"] != plan:
                raise PermissionError("execution_plan_changed")
            cur.execute(
                "UPDATE autonomy_operations SET execution_plan=%s WHERE id=%s",
                (Json(plan), operation_id),
            )

    def wait_for_code(self, scope: Scope, operation_id: str) -> None:
        with self.transaction() as cur:
            self._lock(cur, scope)
            row = self._operation(cur, scope, operation_id)
            if row["state"] != "reserved" or not row["execution_plan"]:
                raise PermissionError("operation_not_prepared")
            cur.execute(
                "UPDATE autonomy_operations SET state='awaiting_input',input_reason='code_before_submit',updated_at=now() "
                "WHERE id=%s",
                (operation_id,),
            )
            self._event(cur, scope, operation_id, "code_required_before_submit")

    def resume_with_code(self, scope: Scope, operation_id: str) -> dict[str, Any]:
        # Called only by the authenticated human endpoint. No code is passed
        # to this DAL, and uncertain submissions cannot enter this transition.
        with self.transaction() as cur:
            self._lock(cur, scope)
            row = self._operation(cur, scope, operation_id)
            if row["state"] != "awaiting_input" or row["input_reason"] != "code_before_submit":
                raise PermissionError("operation_not_waiting_for_code")
            self._policy(cur, scope, row["grant_id"])
            cur.execute(
                "UPDATE autonomy_operations SET state='reserved',input_reason=NULL,updated_at=now() WHERE id=%s",
                (operation_id,),
            )
            self._event(cur, scope, operation_id, "code_resume_requested")
            return row

    def settings(self, scope: Scope) -> RuntimeSettings:
        with self.transaction() as cur:
            cur.execute(
                "SELECT settings FROM autonomy_settings WHERE tenant_id=%s AND owner_id=%s",
                (scope.tenant_id, scope.owner_id),
            )
            row = cur.fetchone()
            return RuntimeSettings.model_validate(row["settings"]) if row else RuntimeSettings()

    def configure(self, scope: Scope, settings: RuntimeSettings) -> None:
        with self.transaction() as cur:
            self._lock(cur, scope)
            cur.execute(
                "INSERT INTO autonomy_settings(tenant_id,owner_id,settings) VALUES (%s,%s,%s) "
                "ON CONFLICT(tenant_id,owner_id) DO UPDATE SET settings=EXCLUDED.settings,updated_at=now()",
                (scope.tenant_id, scope.owner_id, Json(settings.model_dump(mode="json"))),
            )
            self._event(cur, scope, str(uuid4()), "settings_changed")

    def spending_projection(self, scope: Scope) -> dict[str, Any]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT proposal,state,updated_at FROM autonomy_operations "
                "WHERE tenant_id=%s AND owner_id=%s "
                "AND proposal->>'action' IN ('purchase','subscription') "
                "AND state IN ('reserved','submitting','reconciling','awaiting_input','completed')",
                (scope.tenant_id, scope.owner_id),
            )
            rows = list(cur.fetchall())
        currencies = sorted({row["proposal"]["currency"] for row in rows})
        result = {}
        for currency in currencies:
            try:
                result[currency] = monthly_projection(
                    [row for row in rows if row["proposal"]["currency"] == currency],
                    today=datetime.now(UTC).date(),
                )
            except ValueError:
                return {"state": "renewal_schedule_missing", "months": {}}
        return {"state": "ready", "months": result}

    def recent_operations(self, scope: Scope) -> list[dict[str, Any]]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT id::text,state,proposal,evidence,created_at,updated_at FROM autonomy_operations "
                "WHERE tenant_id=%s AND owner_id=%s ORDER BY created_at DESC LIMIT 100",
                (scope.tenant_id, scope.owner_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def finish(
        self, scope: Scope, operation_id: str, state: str, evidence: dict[str, Any] | None = None
    ) -> None:
        allowed = {
            "reserved": {"cancelled", "failed", "awaiting_input"},
            "submitting": {"reconciling", "completed", "awaiting_input"},
            "reconciling": {"completed", "failed", "awaiting_input"},
            "awaiting_input": {"reconciling", "cancelled"},
        }
        # This method is broker-only. Routes/tools never accept arbitrary
        # completion evidence from the model or client.
        if state == "completed" and not evidence:
            raise ValueError("completion_requires_evidence")
        if evidence and (
            set(evidence)
            - {"origin", "confirmation_sha256", "verified_at", "kind", "confirmation_rule"}
        ):
            raise ValueError("unsafe_evidence")
        if (
            evidence
            and "confirmation_rule" in evidence
            and evidence["confirmation_rule"]
            not in {
                "account_created",
                "application_received",
                "order_confirmed",
                "membership_active",
                "login_confirmed",
            }
        ):
            raise ValueError("unsafe_confirmation_rule")
        with self.transaction() as cur:
            self._lock(cur, scope)
            row = self._operation(cur, scope, operation_id)
            if state not in allowed.get(row["state"], set()):
                raise ValueError("invalid_transition")
            cur.execute(
                "UPDATE autonomy_operations SET state=%s,evidence=%s,updated_at=now() WHERE id=%s",
                (state, Json(evidence), operation_id),
            )
            self._event(cur, scope, operation_id, state)
