"""Recover effect admission after the persisted owning run has ended.

This step never retries a business action or declares a dispatched action done.
A provider verifier must still settle any uncertain external effect.
"""

import logging

from robothor.engine.runtime import effects

logger = logging.getLogger(__name__)


def sweep_terminal(tenant_id: str) -> int:
    """Bounded, repeatable housekeeping; failures leave the existing fence intact."""
    try:
        with effects.get_connection() as conn, conn.cursor() as cur:
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
            return cur.rowcount
    except Exception:
        logger.warning("Terminal effect recovery deferred", exc_info=True)
        return 0
