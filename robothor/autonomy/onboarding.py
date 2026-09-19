"""Reuse verified account information without disclosing it to the agent."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import SecretStr

from robothor.autonomy.models import ResourceInput

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


def import_contact_profile(store: AutonomyStore, scope: Scope) -> dict[str, Any]:
    if not scope.owner_id.startswith("person:"):
        raise PermissionError("linked_contact_required")
    try:
        person_id = str(UUID(scope.owner_id.removeprefix("person:")))
    except ValueError:
        raise PermissionError("linked_contact_required") from None
    with store.transaction() as cur:
        cur.execute(
            "SELECT first_name,last_name,email,phone,job_title,city FROM crm_people "
            "WHERE id=%s AND tenant_id=%s AND deleted_at IS NULL",
            (person_id, scope.tenant_id),
        )
        row = cur.fetchone()
    if not row:
        raise PermissionError("linked_contact_not_found")
    mapping = {
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
        "phone": "phone",
        "job_title": "occupation",
        "city": "city",
    }
    profile = {
        target: row[source]
        for source, target in mapping.items()
        if isinstance(row.get(source), str) and row[source].strip()
    }
    if not profile:
        raise ValueError("contact_profile_empty")
    return store.put_resource(
        scope,
        ResourceInput(
            kind="profile", label="Saved contact profile", payload=SecretStr(json.dumps(profile))
        ),
    )
