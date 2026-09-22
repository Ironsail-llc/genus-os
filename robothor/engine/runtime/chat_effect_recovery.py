"""Reconcile one chat-owned CRM effect using stored evidence, never another write."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.auth.deps import AuthContext

import logging
from types import SimpleNamespace

from robothor.db.connection import tenant_scope
from robothor.engine.runtime import effects, note_recovery, task_recovery

logger = logging.getLogger(__name__)


def reconcile_record(effect_id: str, auth: AuthContext) -> None:
    """Caller must select the effect from the authenticated original run family."""
    context = SimpleNamespace(tenant_id=auth.tenant_id, principal_id=auth.user_id)
    try:
        record = effects.read(context, effect_id)
        if not record:
            return
        recover = {"create_note": note_recovery.recover, "create_task": task_recovery.recover}.get(
            record["tool_name"]
        )
        if recover is None:
            return
        if record["state"] in {"prepared", "dispatching"}:
            # A terminal parent does not imply that its delegated worker stopped.
            # Recheck this effect's own durable owner before revoking admission.
            with (
                tenant_scope(context.tenant_id),
                effects.get_connection() as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """UPDATE agent_runtime_effects e SET
                       state=CASE WHEN state='prepared' THEN 'not_applied' ELSE 'uncertain' END,
                       version=version+1,updated_at=now()
                       WHERE e.id=%s AND e.tenant_id=%s AND e.principal_id=%s
                         AND e.state IN ('prepared','dispatching')
                         AND EXISTS (SELECT 1 FROM agent_runs r
                           WHERE r.id::text=e.run_id AND r.tenant_id=e.tenant_id
                             AND r.user_id=e.principal_id
                             AND r.status IN ('completed','failed','timeout','cancelled'))""",
                    (effect_id, context.tenant_id, context.principal_id),
                )
        recover(context, effect_id)
    except Exception as exc:
        # Keep the durable replay fence. A later poll can retry the read.
        logger.warning("Chat CRM recovery deferred (%s)", type(exc).__name__)
