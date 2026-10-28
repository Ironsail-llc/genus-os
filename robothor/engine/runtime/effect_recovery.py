"""Recover terminal-run admission and read back supported uncertain effects.

This step never retries a business action. Only a trusted provider verifier
can settle a dispatched effect as applied or not applied. An uncertain effect
for a tool with no verifier can never be read back, so once it is a day old
the sweep records an unverified reconciliation (an audit entry, not evidence)
instead of letting it wedge admission indefinitely.
"""

import logging
from types import SimpleNamespace

from robothor.db.connection import tenant_scope
from robothor.engine.runtime import effects

logger = logging.getLogger(__name__)

SWEEP_ACTOR = "system:effect-sweep"
UNVERIFIABLE_GRACE = "24 hours"


def has_verifier(tool_name: str) -> bool:
    """Whether a host-owned readback verifier can settle this tool's effects."""
    return tool_name in effects.READBACK_TOOLS


def reconcile_unverifiable(tenant_id: str) -> int:
    """Attest day-old uncertain effects that no verifier could ever settle.

    Goal-owned effects are left alone (``attest`` refuses them): their family
    fence is reconciled by the goal machinery, not by elapsed time.
    """
    with tenant_scope(tenant_id), effects.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id,principal_id,tool_name FROM agent_runtime_effects
               WHERE tenant_id=%s AND state='uncertain' AND goal_id IS NULL
                 AND NOT (tool_name = ANY(%s))
                 AND updated_at < now() - %s::interval
               ORDER BY updated_at,id LIMIT 100""",
            (tenant_id, sorted(effects.READBACK_TOOLS), UNVERIFIABLE_GRACE),
        )
        rows = cur.fetchall()
    settled = 0
    for identifier, principal_id, tool_name in rows:
        scope = SimpleNamespace(tenant_id=tenant_id, principal_id=principal_id)
        note = (
            f"{tool_name} outcome unknown for over {UNVERIFIABLE_GRACE}; no readback "
            "verifier exists, released so later calls are not fenced indefinitely"
        )
        if effects.attest(scope, identifier, actor=SWEEP_ACTOR, note=note):
            settled += 1
    if settled:
        logger.info("Reconciled %d unverifiable uncertain effect(s)", settled)
    return settled


def sweep_terminal(tenant_id: str) -> int:
    """Bounded, repeatable housekeeping; failures leave the existing fence intact."""
    try:
        with tenant_scope(tenant_id), effects.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """WITH abandoned AS (
                    SELECT e.id FROM agent_runtime_effects e
                    JOIN agent_runs r ON r.id::text=e.run_id AND r.tenant_id=e.tenant_id
                    WHERE e.tenant_id=%s AND e.state IN ('prepared','dispatching')
                      AND r.status IN ('completed','failed','timeout','cancelled')
                    ORDER BY e.created_at,e.id LIMIT 100
                    FOR UPDATE OF e SKIP LOCKED
                ) UPDATE agent_runtime_effects e SET
                    state=CASE WHEN e.state='prepared' THEN 'not_applied' ELSE 'uncertain' END,
                    version=e.version+1,updated_at=now()
                    FROM abandoned a WHERE e.id=a.id""",
                (tenant_id,),
            )
            count = cur.rowcount
        from robothor.engine.runtime import note_recovery, task_recovery

        note_recovery.sweep(tenant_id)
        task_recovery.sweep(tenant_id)
        reconcile_unverifiable(tenant_id)
        return int(count)
    except Exception:
        logger.warning("Terminal effect recovery deferred", exc_info=True)
        return 0
