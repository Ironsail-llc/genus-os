"""Value-free field availability and enrollment provenance for personal resources."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from psycopg2.extras import Json

from robothor.autonomy.crypto import open_resource
from robothor.entity.audit import redact_for_audit
from robothor.secrets.redaction import redact

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore

Source = Literal[
    "secure_input",
    "linked_contact",
    "generated",
    "broker_session",
    "mailbox_verification",
    "legacy_enrollment",
]


def describe(
    kind: str, plaintext: str, source: Source, *, recorded_at: datetime | None = None
) -> dict[str, Any]:
    value = json.loads(plaintext)
    if kind == "profile":
        fields = [key for key, item in value.items() if isinstance(item, str) and item.strip()]
        fields += [
            f"answers.{key}" for key, item in value.get("answers", {}).items() if item.strip()
        ]
    elif kind == "document":
        fields = ["file"] if value.get("base64") else []
    elif kind == "browser_session":
        fields = ["session"]
    elif kind == "totp":
        fields = ["code"] if value.get("secret") else []
    else:
        fields = [key for key, item in value.items() if item]
    fields = [key for key in fields if redact_for_audit(key) == key and redact(key) == key]
    return {
        "version": 1,
        "fields": sorted(fields),
        "source": source,
        "recorded_at": (recorded_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }


def refresh_descriptors(store: AutonomyStore, scope: Scope) -> int:
    """Owner maintenance boundary: older records never acquire invented provenance."""
    keys = store.keys
    count = 0
    with store.transaction() as cur:
        cur.execute(
            "SELECT id::text,kind,encrypted_value,created_at FROM vault_resources "
            "WHERE tenant_id=%s AND owner_id=%s AND active AND descriptor='{}'::jsonb "
            "AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE",
            (scope.tenant_id, scope.owner_id),
        )
        for row in cur.fetchall():
            plaintext = open_resource(bytes(row["encrypted_value"]), keys, scope, row["id"])
            descriptor = describe(
                row["kind"], plaintext, "legacy_enrollment", recorded_at=row["created_at"]
            )
            cur.execute(
                "UPDATE vault_resources SET descriptor=%s WHERE id=%s",
                (Json(descriptor), row["id"]),
            )
            store._event(cur, scope, row["id"], "resource_described")
            count += 1
    return count
