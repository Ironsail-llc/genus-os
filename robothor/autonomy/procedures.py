"""Find reusable, reference-only templates in successfully completed operations."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import field_validator

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.confirmation import classify
from robothor.autonomy.models import Action, Scope, StrictModel
from robothor.autonomy.models import origin as validate_origin
from robothor.autonomy.store import declared_plan

if TYPE_CHECKING:
    from robothor.autonomy.store import AutonomyStore


class ProcedureQuery(StrictModel):
    origin: str
    action: Action

    _origin = field_validator("origin")(validate_origin)


def find_procedures(
    store: AutonomyStore, scope: Scope, agent_id: str, query: ProcedureQuery
) -> list[dict[str, Any]]:
    with store.transaction() as cur:
        cur.execute(
            "SELECT id::text,execution_plan,updated_at FROM autonomy_operations "
            "WHERE tenant_id=%s AND owner_id=%s AND agent_id=%s AND state='completed' "
            "AND proposal->>'origin'=%s AND proposal->>'action'=%s "
            "AND evidence->>'kind'='merchant_confirmation' AND execution_plan IS NOT NULL "
            "AND updated_at>now()-interval '90 days' ORDER BY updated_at DESC LIMIT 50",
            (scope.tenant_id, scope.owner_id, agent_id, query.origin, query.action),
        )
        rows = cur.fetchall()
    candidates = []
    resource_ids = set()
    for row in rows:
        try:
            plan = ExecutionPlan.model_validate(declared_plan(row["execution_plan"]))
        except ValueError:
            continue
        if plan.verification_link_id:
            continue
        parsed = urlsplit(plan.url)
        if validate_origin(f"{parsed.scheme}://{parsed.netloc}") != query.origin:
            continue
        template = plan.model_dump(mode="json")
        template["url"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        template["session_resource_id"] = None
        template["verification_link_id"] = None
        # A receipt-specific success marker from the original request is not a
        # reusable assertion about the next request. Fall back to new discovery.
        if not plan.success_text or not classify(plan.success_text, query.action):
            template["success_selector"] = None
            template["success_text"] = None
        ids = {field["resource_id"] for field in template["fields"]}
        resource_ids.update(ids)
        candidates.append((row, template, ids))
    reusable_resources = set()
    if resource_ids:
        with store.transaction() as cur:
            cur.execute(
                "SELECT id::text FROM vault_resources WHERE tenant_id=%s AND owner_id=%s "
                "AND active AND expires_at IS NULL AND id=ANY(%s::uuid[])",
                (scope.tenant_id, scope.owner_id, list(resource_ids)),
            )
            reusable_resources = {row["id"] for row in cur.fetchall()}
    seen = set()
    result = []
    for row, template, ids in candidates:
        if not ids <= reusable_resources:
            continue
        layout = {
            **template,
            "fields": [
                {key: value for key, value in field.items() if key != "resource_id"}
                for field in template["fields"]
            ],
        }
        fingerprint = hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(
            {
                "source_operation_id": row["id"],
                "origin": query.origin,
                "action": query.action,
                "last_verified_at": row["updated_at"].isoformat(),
                "plan_template": template,
                "inspect_before_execution": True,
            }
        )
        if len(result) == 5:
            break
    return result
