"""Find one unambiguous native continuation without crossing audit ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.auth.deps import AuthContext


def continuation(cur: Any, root: dict[str, Any], auth: AuthContext) -> dict[str, Any] | None:
    cur.execute(
        """WITH RECURSIVE chain AS (
            SELECT id FROM agent_runs WHERE id=%s AND tenant_id=%s AND user_id=%s
            UNION
            SELECT child.id FROM agent_runs child JOIN chain parent
              ON child.runtime_context->>'resume_from_run_id'=parent.id::text
            WHERE child.tenant_id=%s AND child.user_id=%s
        ) SELECT r.id,r.agent_id,r.status,r.output_text,r.error_message,r.verified_status,
                 r.trigger_detail,r.runtime_context->>'resume_from_run_id' AS resume_origin
          FROM agent_runs r JOIN chain c ON c.id=r.id""",
        (root["id"], auth.tenant_id, auth.user_id, auth.tenant_id, auth.user_id),
    )
    members = {str(row["id"]): row for row in cur.fetchall()}
    current, seen = str(root["id"]), set()
    if current not in members:
        return None
    while current not in seen:
        seen.add(current)
        children = [key for key, row in members.items() if row["resume_origin"] == current]
        if not children:
            return members[current] if len(seen) == len(members) else None
        if len(children) != 1:
            return None
        current = children[0]
    return None
