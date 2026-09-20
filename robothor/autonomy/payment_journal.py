"""Encrypted append-only personal payment facts; no agent-facing write interface.

Issuer adapters must authenticate and bind external evidence before calling
``append``. A source label alone is not proof. Unsupported evidence is retained
with an unresolved projection, never silently converted into recovered funds.

Two things happen here that are not projection. Evidence of money above the
authority it was given freezes the grant and pages the owner
(``robothor.autonomy.payment_hold``) -- a discrepancy flag on a panel nobody
opens is not a control. And a *refused* append records its refusal on a second
connection, because the refusal is exactly the event worth investigating and
the transaction that would have carried the audit row is the one being rolled
back.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from robothor.autonomy import alerts
from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.payment_groups import summarize_payments
from robothor.autonomy.payment_hold import alert_body, overspend_reasons, place_hold
from robothor.autonomy.payment_lifecycle import PaymentFact

logger = logging.getLogger(__name__)

#: Refusal reasons are code-level tokens (``payment_event_conflict`` and the
#: like), never caller data. The pattern keeps it that way: anything else is
#: recorded as ``unspecified`` rather than copied into the durable journal.
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

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

    def _flag_overspend(
        self, cur: Any, scope: Scope, op: dict[str, Any], summary: dict[str, Any]
    ) -> dict[str, Any]:
        """Freeze the grant and page the owner when money exceeded its authority.

        The freeze is written inside the caller's transaction, atomically with
        the evidence that justifies it. The alert is sent from here rather than
        after the commit: a rolled-back transaction can therefore produce an
        alert for evidence that was not kept, and a retry can alert twice. That
        is the deliberate direction -- a duplicate money alert costs the
        operator a glance, a missing one cost 400x the reservation.
        """
        reasons = overspend_reasons(summary)
        if not reasons or not place_hold(cur, self.store, scope, op["grant_id"], op["id"], reasons):
            return summary
        # Module attribute, not a bound name: the delivery seam is patched at
        # its source, and a from-import here would silently bypass that.
        alerts.notify_owner(
            tenant_id=scope.tenant_id,
            subject="Delegated payment exceeded its authority",
            body=alert_body(op["id"], op["grant_id"], reasons),
        )
        return summary

    def _record_refusal(self, scope: Scope, operation_id: str, exc: BaseException) -> None:
        """Audit a refused append on a fresh transaction, after the rollback.

        Never raises: an audit write that fails must not replace the refusal
        the caller is about to see. The operation is re-resolved for this scope
        first, so a probe from a scope that cannot see the operation writes
        nothing at all.
        """
        reason = str(exc)
        if not _REASON.match(reason):
            reason = "unspecified"
        try:
            with self.store.transaction(scope) as cur:
                self.store._operation(cur, scope, operation_id)
                self.store._event(cur, scope, operation_id, f"payment_evidence_refused:{reason}")
        except PermissionError:
            return
        except Exception:
            logger.exception(
                "a refused payment append on operation %s could not be audited", operation_id
            )

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
            # A retry after a rolled-back append re-places a lost hold.
            return self._flag_overspend(cur, scope, op, self._summary(op, facts))
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
        return self._flag_overspend(cur, scope, op, self._summary(op, [*facts, fact]))

    def append(self, scope: Scope, operation_id: str, fact: PaymentFact) -> dict[str, Any]:
        """Trusted adapter boundary, deliberately unavailable through tools/routes."""
        try:
            with self.store.transaction(scope) as cur:
                self.store._lock(cur, scope)
                op = self.store._operation(cur, scope, operation_id)
                cur.execute(
                    "SELECT 1 FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s "
                    "AND subject_id=%s AND event IN ('submitting','external_action_requested') "
                    "LIMIT 1",
                    (scope.tenant_id, scope.owner_id, operation_id),
                )
                if not cur.fetchone():
                    raise PermissionError("payment_submission_not_started")
                if fact.source == "merchant" and op["state"] != "completed":
                    raise PermissionError("submission_confirmation_required")
                return self._append(cur, scope, op, fact)
        except (PermissionError, ValueError) as exc:
            # The refusal is the event worth investigating, and the rollback
            # took its audit row with it. Re-open and record it.
            self._record_refusal(scope, operation_id, exc)
            raise

    def read(self, scope: Scope, operation_id: str) -> dict[str, Any]:
        with self.store.transaction(scope) as cur:
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
