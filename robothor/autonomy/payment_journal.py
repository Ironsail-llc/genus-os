"""Encrypted append-only personal payment facts; no agent-facing write interface.

Issuer adapters must authenticate and bind external evidence before calling
``append``. A source label alone is not proof. Unsupported evidence is retained
with an unresolved projection, never silently converted into recovered funds.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.payment_groups import summarize_payments
from robothor.autonomy.payment_lifecycle import PaymentFact

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


def _binding(operation_id: str, record_id: str, version: int) -> str:
    return f"payment:{operation_id}:{record_id}:{version}"


class PaymentJournal:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    def _facts(self, cur: Any, scope: Scope, operation_id: str) -> list[PaymentFact]:
        cur.execute(
            "SELECT id::text,version,encrypted_value FROM autonomy_payment_events "
            "WHERE tenant_id=%s AND owner_id=%s AND operation_id=%s ORDER BY version",
            (scope.tenant_id, scope.owner_id, operation_id),
        )
        rows = cur.fetchall()
        keys = self.store.keys
        result = []
        for expected, row in enumerate(rows, 1):
            if row["version"] != expected:
                raise PermissionError("payment_journal_incomplete")
            try:
                value = open_resource(
                    bytes(row["encrypted_value"]),
                    keys,
                    scope,
                    _binding(operation_id, row["id"], expected),
                )
                result.append(PaymentFact.model_validate_json(value))
            except Exception:
                raise PermissionError("payment_evidence_unavailable") from None
        return result

    @staticmethod
    def _summary(op: dict[str, Any], facts: list[PaymentFact]) -> dict[str, Any]:
        return summarize_payments(op, facts)

    def _append(
        self, cur: Any, scope: Scope, op: dict[str, Any], fact: PaymentFact
    ) -> dict[str, Any]:
        fact = PaymentFact.model_validate(fact.model_dump())
        if op["proposal"]["action"] not in {"purchase", "subscription"}:
            raise PermissionError("payment_operation_required")
        if fact.source == "merchant" and (fact.kind != "submitted" or fact.amount_minor):
            raise PermissionError("issuer_evidence_required")
        facts = self._facts(cur, scope, op["id"])
        previous = next((row for row in facts if row.event_key == fact.event_key), None)
        if previous is not None:
            if previous != fact:
                raise ValueError("payment_event_conflict")
            return self._summary(op, facts)
        record_id = str(uuid4())
        version = len(facts) + 1
        key_id, keys = self.store.resource_keyring()
        encrypted = seal_resource(
            fact.model_dump_json(), keys, key_id, scope, _binding(op["id"], record_id, version)
        )
        digest = hashlib.sha256(fact.event_key.encode()).hexdigest()
        cur.execute(
            "INSERT INTO autonomy_payment_events "
            "(id,tenant_id,owner_id,operation_id,version,event_key_digest,encrypted_value) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (record_id, scope.tenant_id, scope.owner_id, op["id"], version, digest, encrypted),
        )
        self.store._event(cur, scope, op["id"], "payment_evidence_recorded")
        return self._summary(op, [*facts, fact])

    def append(self, scope: Scope, operation_id: str, fact: PaymentFact) -> dict[str, Any]:
        """Trusted adapter boundary, deliberately unavailable through tools/routes."""
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            op = self.store._operation(cur, scope, operation_id)
            cur.execute(
                "SELECT 1 FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s "
                "AND subject_id=%s AND event IN ('submitting','external_action_requested') LIMIT 1",
                (scope.tenant_id, scope.owner_id, operation_id),
            )
            if not cur.fetchone():
                raise PermissionError("payment_submission_not_started")
            if fact.source == "merchant" and op["state"] != "completed":
                raise PermissionError("submission_confirmation_required")
            return self._append(cur, scope, op, fact)

    def read(self, scope: Scope, operation_id: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
            op = self.store._operation(cur, scope, operation_id)
            return self._summary(op, self._facts(cur, scope, operation_id))


def record_submission(cur: Any, store: AutonomyStore, scope: Scope, op: dict[str, Any]) -> None:
    """Called in the broker's completion transaction; never a settlement claim."""
    if op["proposal"]["action"] in {"purchase", "subscription"}:
        PaymentJournal(store)._append(
            cur,
            scope,
            op,
            PaymentFact(
                event_key="broker-submission",
                kind="submitted",
                amount_minor=0,
                currency=op["proposal"]["currency"],
                source="merchant",
            ),
        )
