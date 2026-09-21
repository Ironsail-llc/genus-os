"""Durable effect attempts, independent of worker memory and checkpoint versions.

The host must reserve before dispatch and finish only after a classified result.
Uncertainty is resolved by a trusted provider verifier, never by a model flag or
an absence observed while the provider may still complete the original request.
Arguments are fingerprinted, not copied into this store (they may hold secrets).
"""

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor

from robothor.db.connection import get_connection

active_effect: ContextVar[dict | None] = ContextVar("active_effect", default=None)


class EffectPendingError(ValueError):
    def __init__(self, effect_id):
        self.effect_id = str(effect_id)
        super().__init__(
            "An earlier action is unresolved; audit/readback is required before another write"
        )


@dataclass(frozen=True)
class Verification:
    outcome: str
    settled: bool = False
    reference: str = ""
    result: dict | None = None


def fingerprint(tool_name, arguments):
    if tool_name == "create_task" and isinstance(arguments, dict):
        # Presentation does not change the business action or its retry identity.
        arguments = {key: value for key, value in arguments.items() if key != "finalReport"}
    payload = json.dumps(
        [tool_name, arguments], sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _lock(cur, context):
    if context.goal_id:
        # Serialize admission against family completion/reconciliation checks.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + context.tenant_id,))
    scope = json.dumps([context.tenant_id, context.principal_id]).encode()
    key = int.from_bytes(hashlib.sha256(scope).digest()[:8], "big", signed=True)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def begin(context, run_id, agent_id, tool_name, arguments):
    """Reserve one dispatch or return an already reconciled result for this request."""
    digest = fingerprint(tool_name, arguments)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _lock(cur, context)
        cur.execute(
            """SELECT * FROM agent_runtime_effects WHERE tenant_id=%s AND principal_id=%s
               AND request_id=%s AND fingerprint=%s AND (state='confirmed'
                 OR state='finished' AND resolution->>'source'='tool_response')
               ORDER BY created_at DESC LIMIT 1""",
            (context.tenant_id, context.principal_id, context.request_id, digest),
        )
        confirmed = cur.fetchone()
        if confirmed:
            return dict(confirmed)
        cur.execute(
            """SELECT id FROM agent_runtime_effects WHERE tenant_id=%s AND principal_id=%s
               AND (state IN ('prepared','dispatching','uncertain') AND fingerprint=%s
                 OR state='uncertain' AND (request_id=%s OR goal_id=%s OR budget_id=%s))
               ORDER BY created_at LIMIT 1""",
            (
                context.tenant_id,
                context.principal_id,
                digest,
                context.request_id,
                context.goal_id,
                context.budget_id,
            ),
        )
        pending = cur.fetchone()
        if pending:
            raise EffectPendingError(pending["id"])
        identifier = str(uuid4())
        cur.execute(
            """INSERT INTO agent_runtime_effects
               (id,tenant_id,principal_id,request_id,run_id,agent_id,goal_id,budget_id,
                tool_name,fingerprint,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared')
               RETURNING *""",
            (
                identifier,
                context.tenant_id,
                context.principal_id,
                context.request_id,
                run_id,
                agent_id,
                context.goal_id,
                context.budget_id,
                tool_name,
                digest,
            ),
        )
        return dict(cur.fetchone())


def mark_dispatched(context, effect_id, run_id):
    """Only the owner of a still-prepared record may cross the dispatch boundary."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state='dispatching',version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND request_id=%s
                 AND run_id=%s AND state='prepared'""",
            (effect_id, context.tenant_id, context.principal_id, context.request_id, run_id),
        )
        return cur.rowcount == 1


def finish(context, effect_id, run_id, *, uncertain):
    """A late worker cannot overwrite a recovery verdict or another worker's record."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=%s,version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND request_id=%s
                 AND run_id=%s AND state IN ('prepared','dispatching')""",
            (
                "uncertain" if uncertain else "finished",
                effect_id,
                context.tenant_id,
                context.principal_id,
                context.request_id,
                run_id,
            ),
        )
        return cur.rowcount == 1


def abandon_run(context, run_id):
    """The host observed worker loss: surviving dispatch intents now require readback."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=CASE WHEN state='prepared' THEN 'not_applied' ELSE 'uncertain' END,
               version=version+1,updated_at=now()
               WHERE tenant_id=%s AND principal_id=%s AND run_id=%s AND state IN ('prepared','dispatching')""",
            (context.tenant_id, context.principal_id, run_id),
        )
        return cur.rowcount


def read(context, effect_id):
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM agent_runtime_effects WHERE id=%s AND tenant_id=%s AND principal_id=%s",
            (effect_id, context.tenant_id, context.principal_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def resolve(context, effect_id, verifier):
    """Invoke a host-owned provider verifier outside the database transaction.

    A not-applied verdict must be definitive: an eventually consistent missing
    resource, or an in-flight request, does not establish that no effect can occur.
    This is a host extension interface, not a tool or model-supplied callback.
    """
    record = read(context, effect_id)
    if not record or record["state"] != "uncertain":
        return False
    verdict = verifier(record)
    if not isinstance(verdict, Verification) or verdict.outcome not in {
        "applied",
        "not_applied",
        "unknown",
    }:
        raise ValueError("invalid provider verification")
    if verdict.outcome == "unknown" or verdict.settled is not True:
        return False
    if not verdict.reference or not isinstance(verdict.reference, str):
        raise ValueError("provider evidence reference required")
    if verdict.outcome == "applied" and (
        not isinstance(verdict.result, dict) or verdict.result.get("error")
    ):
        raise ValueError("verified result required")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runtime_effects SET state=%s,resolution=%s,version=version+1,updated_at=now()
               WHERE id=%s AND tenant_id=%s AND principal_id=%s AND version=%s AND state='uncertain'""",
            (
                "confirmed" if verdict.outcome == "applied" else "not_applied",
                Json({"reference": verdict.reference, "result": verdict.result}),
                effect_id,
                context.tenant_id,
                context.principal_id,
                record["version"],
            ),
        )
        return cur.rowcount == 1
