"""Expiring intake intents; a token never substitutes for an authenticated owner."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import model_validator

from robothor.autonomy.models import ResourceInput, StrictModel, origin

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


class EnrollmentRequest(StrictModel):
    kind: Literal["profile", "credential", "document", "totp", "payment_card"]
    origin: str | None = None

    @model_validator(mode="after")
    def destination(self) -> EnrollmentRequest:
        if self.kind in {"credential", "totp"} and not self.origin:
            raise ValueError("website_required")
        if self.origin:
            object.__setattr__(self, "origin", origin(self.origin))
        return self


class EnrollmentStore:
    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    @staticmethod
    def _digest(token: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{43}", token):
            raise PermissionError("enrollment_unavailable")
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, scope: Scope, request: EnrollmentRequest) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as cur:
            cur.execute(
                "INSERT INTO autonomy_enrollments "
                "(id,tenant_id,owner_id,token_hash,kind,origin,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,now()+interval '15 minutes') RETURNING expires_at",
                (
                    str(uuid4()),
                    scope.tenant_id,
                    scope.owner_id,
                    self._digest(token),
                    request.kind,
                    request.origin,
                ),
            )
            expires = cur.fetchone()["expires_at"].isoformat()
        from robothor.settings import get_settings

        configured = get_settings().autonomy.dashboard_origin
        path = "/account/autonomy#enroll=" + token
        return {
            "token": token,
            "path": path,
            "url": origin(configured) + path if configured else None,
            "expires_at": expires,
        }

    def _read(self, cur: Any, scope: Scope, token: str) -> dict[str, Any]:
        cur.execute(
            "SELECT kind,origin,expires_at,resource_id::text FROM autonomy_enrollments "
            "WHERE token_hash=%s AND tenant_id=%s AND owner_id=%s AND expires_at>now() FOR UPDATE",
            (self._digest(token), scope.tenant_id, scope.owner_id),
        )
        row = cur.fetchone()
        if not row:
            raise PermissionError("enrollment_unavailable")
        return {**row, "expires_at": row["expires_at"].isoformat()}

    def inspect(self, scope: Scope, token: str) -> dict[str, Any]:
        with self.store.transaction() as cur:
            return self._read(cur, scope, token)

    def complete(self, scope: Scope, token: str, resource: ResourceInput) -> dict[str, Any]:
        # Seal before taking the intent lock; key discovery may use its own connection.
        prepared = self.store.prepare_resource(scope, resource)
        with self.store.transaction() as cur:
            row = self._read(cur, scope, token)
            if resource.kind != row["kind"] or resource.origin != row["origin"]:
                raise PermissionError("enrollment_scope_mismatch")
            if row["resource_id"]:
                # A retry returns the original reference; it cannot replace its value.
                cur.execute(
                    "SELECT id::text,kind,label,origin,descriptor FROM vault_resources "
                    "WHERE id=%s AND tenant_id=%s AND owner_id=%s AND active",
                    (row["resource_id"], scope.tenant_id, scope.owner_id),
                )
                saved = cur.fetchone()
                if not saved:
                    raise PermissionError("enrollment_unavailable")
                return dict(saved)
            receipt = self.store.insert_resource(cur, scope, prepared)
            cur.execute(
                "UPDATE autonomy_enrollments SET resource_id=%s,completed_at=now() "
                "WHERE token_hash=%s AND tenant_id=%s AND owner_id=%s",
                (receipt["id"], self._digest(token), scope.tenant_id, scope.owner_id),
            )
            return receipt
