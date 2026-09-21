"""Encrypted, append-only broker observations; these are not user signatures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.models import Scope, StrictModel
from robothor.autonomy.models import origin as validate_origin

if TYPE_CHECKING:
    from robothor.autonomy.store import AutonomyStore


class TermsDocument(StrictModel):
    origin: str
    text: str = Field(max_length=200_000)
    links: list[str] = Field(default_factory=list, max_length=100)
    source: Literal["rendered", "linked_document"] = "rendered"
    source_url: str | None = Field(default=None, max_length=2000)
    requested_url: str | None = Field(default=None, max_length=2000)
    text_truncated: bool = False
    links_truncated: bool = False

    _origin = field_validator("origin")(validate_origin)

    @model_validator(mode="after")
    def source_bound(self) -> TermsDocument:
        if self.source == "linked_document":
            parsed = urlsplit(self.source_url or "")
            if validate_origin(f"{parsed.scheme}://{parsed.netloc}") != self.origin:
                raise ValueError("document_origin_mismatch")
            requested = urlsplit(self.requested_url or "")
            validate_origin(f"{requested.scheme}://{requested.netloc}")
        elif self.source_url is not None or self.requested_url is not None:
            raise ValueError("unexpected_source_url")
        return self

    @field_validator("links")
    @classmethod
    def bounded_links(cls, links: list[str]) -> list[str]:
        if any(len(link) > 2000 for link in links):
            raise ValueError("link_too_long")
        return links


class TermsSnapshot(StrictModel):
    origin: str
    phase: Literal["before_input", "before_submit", "after_confirmation"]
    documents: list[TermsDocument] = Field(min_length=1, max_length=26)
    confirmation_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    coverage: Literal[
        "visible_text_only", "visible_text_and_selected_documents", "suppressed_after_code"
    ] = "visible_text_only"
    capture_status: Literal["captured", "withheld_after_code", "unavailable"] = "captured"
    omitted_frames: int = Field(default=0, ge=0)

    _origin = field_validator("origin")(validate_origin)

    @model_validator(mode="after")
    def receipt_proof(self) -> TermsSnapshot:
        if (self.phase == "after_confirmation") != bool(self.confirmation_sha256):
            raise ValueError("receipt_confirmation_required")
        if self.capture_status != "captured" and (
            self.phase != "after_confirmation"
            or any(doc.text or doc.links or doc.source != "rendered" for doc in self.documents)
        ):
            raise ValueError("withheld_receipt_cannot_contain_page_data")
        # ``suppressed_after_code`` records the FACT that a pre-click observation
        # was refused after transient-code entry. It carries no page data, and it
        # is a pre-submission coverage value: a receipt uses ``capture_status``.
        if self.coverage == "suppressed_after_code" and (
            self.phase == "after_confirmation"
            or any(doc.text or doc.links or doc.source != "rendered" for doc in self.documents)
        ):
            raise ValueError("suppressed_terms_cannot_contain_page_data")
        return self


_COLUMNS = (
    "id::text,version,grant_version,phase,coverage,document_count,created_at::text,"
    "redacted_at::text"
)


class TermsAudit:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    def _authorize(
        self, cur: Any, scope: Scope, op: dict[str, Any], agent_id: str, snapshot: TermsSnapshot
    ) -> int:
        if op["agent_id"] != agent_id or snapshot.origin != op["proposal"]["origin"]:
            raise PermissionError("audit_not_authorized")
        if snapshot.phase == "after_confirmation":
            if (
                op["state"] != "completed"
                or op["proposal"]["action"] not in {"purchase", "subscription"}
                or (op["evidence"] or {}).get("confirmation_sha256") != snapshot.confirmation_sha256
                or snapshot.coverage != "visible_text_only"
                or any(
                    doc.origin != snapshot.origin or doc.source != "rendered"
                    for doc in snapshot.documents
                )
            ):
                raise PermissionError("audit_not_authorized")
            # Reading a completed payment's receipt does not spend or extend authority.
            return int(op["grant_version"])
        if op["state"] not in {"reserved", "submitting"}:
            raise PermissionError("audit_not_authorized")
        policy, version = self.store._policy(cur, scope, op["grant_id"])
        if version != op["grant_version"] or any(
            (doc.origin not in policy.frame_origins | {snapshot.origin})
            and not (
                doc.source == "linked_document"
                and (policy.allow_any_website or doc.origin in policy.origins)
            )
            for doc in snapshot.documents
        ):
            raise PermissionError("audit_not_authorized")
        return version

    def record(
        self, scope: Scope, operation_id: str, agent_id: str, snapshot: TermsSnapshot
    ) -> dict[str, Any]:
        # Revalidate constructed/copied models too. No caller-supplied metadata.
        snapshot = TermsSnapshot.model_validate(snapshot.model_dump())
        payload = snapshot.model_dump_json()
        if len(payload.encode()) > 5_000_000:
            raise ValueError("terms_snapshot_too_large")
        record_id = str(uuid4())
        key_id, keys = self.store.resource_keyring()
        sealed = seal_resource(payload, keys, key_id, scope, f"terms:{operation_id}:{record_id}")
        with self.store.transaction(scope) as cur:
            self.store._lock(cur, scope)
            op = self.store._operation(cur, scope, operation_id)
            version = self._authorize(cur, scope, op, agent_id, snapshot)
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
            if snapshot.phase == "after_confirmation":
                event = "receipt_recorded"
            elif snapshot.coverage == "suppressed_after_code":
                event = "terms_suppressed"
            else:
                event = "terms_recorded"
            self.store._event(cur, scope, operation_id, event)
        return result

    def list(self, scope: Scope, operation_id: str) -> list[dict[str, Any]]:
        with self.store.transaction(scope) as cur:
            self.store._operation(cur, scope, operation_id)
            cur.execute(
                "SELECT "
                + _COLUMNS
                + " FROM autonomy_terms_snapshots WHERE tenant_id=%s AND owner_id=%s AND operation_id=%s ORDER BY version",
                (scope.tenant_id, scope.owner_id, operation_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def read(self, scope: Scope, operation_id: str, record_id: str) -> dict[str, Any]:
        with self.store.transaction(scope) as cur:
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
        if result.get("redacted_at"):
            # Erased by its owner. The row is deliberately still here — the
            # audit fact is that a snapshot of this phase was taken at this
            # time under this grant version — and there is nothing left to
            # open, which is not the same thing as a key that cannot decrypt.
            result["snapshot"] = None
            return result
        try:
            text = open_resource(
                encrypted, self.store.keys, scope, f"terms:{operation_id}:{record_id}"
            )
            result["snapshot"] = TermsSnapshot.model_validate_json(text).model_dump(mode="json")
        except Exception:
            raise ValueError("audit_unavailable") from None
        return result

    def forget(self, scope: Scope, operation_id: str) -> int:
        """Erase the observed pages for one operation, keeping that it observed.

        The owner's own delete. `autonomy_terms_snapshots` held the rendered
        review page — their name, date of birth, address and the answers they
        typed — with no way to remove it and no expiry. Dropping the whole row
        would let an operation deny that it ever looked at anything, so the
        row, its version, its phase and its grant version stay and only the
        sealed value goes.

        Returns the number of records erased by THIS call; erasing an already
        erased operation is not an error and does not move the stamp.
        """
        with self.store.transaction(scope) as cur:
            # Scoped by the WHERE clause rather than by `_operation`: a delete
            # must not tell a caller whether an operation it cannot see exists.
            cur.execute(
                "UPDATE autonomy_terms_snapshots SET encrypted_value=''::bytea,"
                "redacted_at=now() WHERE tenant_id=%s AND owner_id=%s AND operation_id=%s "
                "AND redacted_at IS NULL",
                (scope.tenant_id, scope.owner_id, operation_id),
            )
            erased = int(cur.rowcount or 0)
            if erased:
                self.store._event(cur, scope, operation_id, "terms_forgotten")
            return erased
