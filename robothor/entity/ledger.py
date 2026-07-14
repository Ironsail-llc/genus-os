"""Append-only, hash-chained treasury event boundary.

The in-memory implementation is intentionally small and useful for unit tests
and single-process development.  Production storage adapters must implement the
same append-only contract and a uniqueness constraint on the scoped
idempotency key; no update or delete operation exists in this interface.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from robothor.entity.audit import AuditValue, redact_for_audit
from robothor.entity.payments import Identifier  # noqa: TC001 - Pydantic runtime field
from robothor.entity.policy import IdempotencyKey  # noqa: TC001 - Pydantic runtime field

_GENESIS_HASH = "0" * 64


class TreasuryEventType(StrEnum):
    PROPOSAL_EVALUATED = "proposal_evaluated"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    AUTHORIZATION_REQUESTED = "authorization_requested"
    AUTHORIZATION_RECORDED = "authorization_recorded"
    SETTLEMENT_RECORDED = "settlement_recorded"
    REVERSAL_RECORDED = "reversal_recorded"


class LedgerEventDraft(BaseModel):
    """A redacted event ready to be appended.

    ``audit_data`` is sanitized during validation, so even a draft's repr or a
    validation-error log cannot retain raw payment material.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: Identifier
    organization_id: Identifier
    event_type: TreasuryEventType
    actor_id: Identifier
    subject_id: Identifier
    idempotency_key: IdempotencyKey
    audit_data: dict[str, AuditValue] = Field(default_factory=dict, repr=False)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("audit_data", mode="before")
    @classmethod
    def sanitize_audit_data(cls, value: object) -> dict[str, AuditValue]:
        redacted = redact_for_audit(value)
        if not isinstance(redacted, dict):
            raise ValueError("audit_data must be an object")
        return redacted

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    sequence: int
    event_id: str
    tenant_id: str
    organization_id: str
    event_type: TreasuryEventType
    actor_id: str
    subject_id: str
    idempotency_key: str
    occurred_at: datetime
    previous_hash: str
    event_hash: str
    _audit_data_json: str = field(repr=False)

    @property
    def audit_data(self) -> dict[str, AuditValue]:
        """Return a new decoded copy; callers cannot mutate stored event data."""

        return cast("dict[str, AuditValue]", json.loads(self._audit_data_json))


class LedgerIdempotencyConflictError(ValueError):
    """The same scoped key was used for a semantically different event."""


@runtime_checkable
class AppendOnlyTreasuryLedger(Protocol):
    def append(self, draft: LedgerEventDraft) -> LedgerEvent: ...

    def events(self) -> tuple[LedgerEvent, ...]: ...

    def find_by_idempotency(
        self, tenant_id: str, organization_id: str, idempotency_key: str
    ) -> LedgerEvent | None: ...


def _canonical_audit_data(data: dict[str, AuditValue]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _semantic_hash(draft: LedgerEventDraft, audit_json: str) -> str:
    semantic = {
        "tenant_id": draft.tenant_id,
        "organization_id": draft.organization_id,
        "event_type": draft.event_type.value,
        "actor_id": draft.actor_id,
        "subject_id": draft.subject_id,
        "idempotency_key": draft.idempotency_key,
        "audit_data": audit_json,
    }
    canonical = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _event_hash(
    *,
    sequence: int,
    tenant_id: str,
    organization_id: str,
    event_type: TreasuryEventType,
    actor_id: str,
    subject_id: str,
    idempotency_key: str,
    occurred_at: datetime,
    previous_hash: str,
    audit_data_json: str,
) -> str:
    material = {
        "sequence": sequence,
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "event_type": event_type.value,
        "actor_id": actor_id,
        "subject_id": subject_id,
        "idempotency_key": idempotency_key,
        "occurred_at": occurred_at.isoformat(),
        "previous_hash": previous_hash,
        "audit_data": audit_data_json,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class InMemoryAppendOnlyTreasuryLedger:
    """Thread-safe append-only ledger with idempotency and tamper evidence."""

    def __init__(self) -> None:
        self._events: list[LedgerEvent] = []
        self._semantic_hashes: dict[tuple[str, str, str], str] = {}
        self._by_idempotency: dict[tuple[str, str, str], LedgerEvent] = {}
        self._lock = threading.Lock()

    def append(self, draft: LedgerEventDraft) -> LedgerEvent:
        # Re-sanitize at the storage boundary.  Pydantic's ``model_copy`` and
        # ``model_construct`` intentionally skip validation, so a ledger
        # adapter must never trust that a draft was built through normal model
        # validation.
        redacted = redact_for_audit(draft.audit_data)
        if not isinstance(redacted, dict):
            raise ValueError("audit_data must be an object")
        audit_json = _canonical_audit_data(redacted)
        semantic_hash = _semantic_hash(draft, audit_json)
        key = (draft.tenant_id, draft.organization_id, draft.idempotency_key)
        with self._lock:
            existing = self._by_idempotency.get(key)
            if existing is not None:
                if self._semantic_hashes[key] != semantic_hash:
                    raise LedgerIdempotencyConflictError(
                        "idempotency key already identifies a different treasury event"
                    )
                return existing

            sequence = len(self._events) + 1
            previous_hash = self._events[-1].event_hash if self._events else _GENESIS_HASH
            event_hash = _event_hash(
                sequence=sequence,
                tenant_id=draft.tenant_id,
                organization_id=draft.organization_id,
                event_type=draft.event_type,
                actor_id=draft.actor_id,
                subject_id=draft.subject_id,
                idempotency_key=draft.idempotency_key,
                occurred_at=draft.occurred_at,
                previous_hash=previous_hash,
                audit_data_json=audit_json,
            )
            event = LedgerEvent(
                sequence=sequence,
                event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, event_hash)),
                tenant_id=draft.tenant_id,
                organization_id=draft.organization_id,
                event_type=draft.event_type,
                actor_id=draft.actor_id,
                subject_id=draft.subject_id,
                idempotency_key=draft.idempotency_key,
                occurred_at=draft.occurred_at,
                previous_hash=previous_hash,
                event_hash=event_hash,
                _audit_data_json=audit_json,
            )
            self._events.append(event)
            self._semantic_hashes[key] = semantic_hash
            self._by_idempotency[key] = event
            return event

    def events(self) -> tuple[LedgerEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def find_by_idempotency(
        self, tenant_id: str, organization_id: str, idempotency_key: str
    ) -> LedgerEvent | None:
        with self._lock:
            return self._by_idempotency.get((tenant_id, organization_id, idempotency_key))

    def verify_chain(self) -> bool:
        with self._lock:
            previous_hash = _GENESIS_HASH
            for expected_sequence, event in enumerate(self._events, start=1):
                if event.sequence != expected_sequence or event.previous_hash != previous_hash:
                    return False
                expected_hash = _event_hash(
                    sequence=event.sequence,
                    tenant_id=event.tenant_id,
                    organization_id=event.organization_id,
                    event_type=event.event_type,
                    actor_id=event.actor_id,
                    subject_id=event.subject_id,
                    idempotency_key=event.idempotency_key,
                    occurred_at=event.occurred_at,
                    previous_hash=event.previous_hash,
                    audit_data_json=event._audit_data_json,
                )
                if event.event_hash != expected_hash:
                    return False
                previous_hash = event.event_hash
            return True


__all__ = [
    "AppendOnlyTreasuryLedger",
    "InMemoryAppendOnlyTreasuryLedger",
    "LedgerEvent",
    "LedgerEventDraft",
    "LedgerIdempotencyConflictError",
    "TreasuryEventType",
]
