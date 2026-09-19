"""Encrypted, append-only broker observations; these are not user signatures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.models import Scope, StrictModel
from robothor.autonomy.models import origin as validate_origin

if TYPE_CHECKING:
    from robothor.autonomy.store import AutonomyStore


class TermsDocument(StrictModel):
    origin: str
    text: str = Field(max_length=200_000)
    links: list[str] = Field(default_factory=list, max_length=100)
    text_truncated: bool = False
    links_truncated: bool = False

    _origin = field_validator("origin")(validate_origin)

    @field_validator("links")
    @classmethod
    def bounded_links(cls, links: list[str]) -> list[str]:
        if any(len(link) > 2000 for link in links):
            raise ValueError("link_too_long")
        return links


class TermsSnapshot(StrictModel):
    origin: str
    phase: Literal["before_input", "before_submit"]
    documents: list[TermsDocument] = Field(min_length=1, max_length=21)
    coverage: Literal["visible_text_only"] = "visible_text_only"
    omitted_frames: int = Field(default=0, ge=0)

    _origin = field_validator("origin")(validate_origin)


_COLUMNS = "id::text,version,grant_version,phase,coverage,document_count,created_at::text"


class TermsAudit:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    def record(
        self, scope: Scope, operation_id: str, agent_id: str, snapshot: TermsSnapshot
    ) -> dict[str, Any]:
        # Revalidate constructed/copied models too. No caller-supplied metadata.
        snapshot = TermsSnapshot.model_validate(snapshot.model_dump())
        payload = snapshot.model_dump_json()
        if len(payload.encode()) > 1_000_000:
            raise ValueError("terms_snapshot_too_large")
        record_id = str(uuid4())
        key_id, keys = self.store.resource_keyring()
        sealed = seal_resource(payload, keys, key_id, scope, f"terms:{operation_id}:{record_id}")
        with self.store.transaction() as cur:
            self.store._lock(cur, scope)
            op = self.store._operation(cur, scope, operation_id)
            if op["agent_id"] != agent_id or op["state"] not in {"reserved", "submitting"}:
                raise PermissionError("audit_not_authorized")
            policy, version = self.store._policy(cur, scope, op["grant_id"])
            if version != op["grant_version"] or snapshot.origin != op["proposal"]["origin"]:
                raise PermissionError("audit_not_authorized")
            if any(
                doc.origin not in policy.frame_origins | {snapshot.origin}
                for doc in snapshot.documents
            ):
                raise PermissionError("audit_not_authorized")
            cur.execute(
                "SELECT COALESCE(max(version),0)+1 AS next FROM autonomy_terms_snapshots WHERE operation_id=%s",
                (operation_id,),
            )
            next_version = cur.fetchone()["next"]
            cur.execute(
                "INSERT INTO autonomy_terms_snapshots(id,tenant_id,owner_id,operation_id,version,grant_version,phase,coverage,document_count,encrypted_value) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING "
                + _COLUMNS,
                (
                    record_id,
                    scope.tenant_id,
                    scope.owner_id,
                    operation_id,
                    next_version,
                    version,
                    snapshot.phase,
                    snapshot.coverage,
                    len(snapshot.documents),
                    sealed,
                ),
            )
            result = dict(cur.fetchone())
            self.store._event(cur, scope, operation_id, "terms_recorded")
        return result

    def list(self, scope: Scope, operation_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as cur:
            self.store._operation(cur, scope, operation_id)
            cur.execute(
                "SELECT "
                + _COLUMNS
                + " FROM autonomy_terms_snapshots WHERE tenant_id=%s AND owner_id=%s AND operation_id=%s ORDER BY version",
                (scope.tenant_id, scope.owner_id, operation_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def read(self, scope: Scope, operation_id: str, record_id: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
            self.store._operation(cur, scope, operation_id)
            cur.execute(
                "SELECT "
                + _COLUMNS
                + ",encrypted_value FROM autonomy_terms_snapshots WHERE id=%s AND tenant_id=%s AND owner_id=%s AND operation_id=%s",
                (record_id, scope.tenant_id, scope.owner_id, operation_id),
            )
            row = cur.fetchone()
            if not row:
                raise PermissionError("audit_not_found")
        result = dict(row)
        encrypted = bytes(result.pop("encrypted_value"))
        try:
            text = open_resource(
                encrypted, self.store.keys, scope, f"terms:{operation_id}:{record_id}"
            )
            result["snapshot"] = TermsSnapshot.model_validate_json(text).model_dump(mode="json")
        except Exception:
            raise ValueError("audit_unavailable") from None
        return result
