"""Conservative candidate crash recovery; never resume or repeat a business action."""

from dataclasses import asdict

from psycopg2.extras import Json

from robothor.db.connection import get_connection
from robothor.engine.runtime.contracts import Usage


def recover_expired(tenant):
    unknown = Json(asdict(Usage(None, None, None, None)))
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """WITH expired AS (
                SELECT id,runtime_context FROM agent_runs
                WHERE tenant_id=%s AND status='running'
                  AND runtime_context->>'runtime_id' IN ('pydantic-ai','deepagents')
                  AND (runtime_context->>'deadline')::timestamptz<=now()
                FOR UPDATE SKIP LOCKED
            ), stops AS (
                INSERT INTO agent_runtime_controls(tenant_id,run_id,action,note)
                SELECT %s,id,'cancel','Expired candidate worker; reconcile effects' FROM expired
                ON CONFLICT(tenant_id,run_id) DO UPDATE SET action='cancel',
                    note=EXCLUDED.note,version=agent_runtime_controls.version+1
            ), request_stops AS (
                INSERT INTO agent_runtime_request_stops(tenant_id,request_id,note)
                SELECT DISTINCT %s,runtime_context->>'request_id',
                    'Expired candidate worker; no implicit replay' FROM expired
                WHERE NULLIF(runtime_context->>'request_id','') IS NOT NULL
                ON CONFLICT(tenant_id,request_id) DO NOTHING
            ) UPDATE agent_runs a SET status='timeout',completed_at=now(),
                duration_ms=GREATEST(0,(EXTRACT(EPOCH FROM now()-a.started_at)*1000)::int),
                error_message='Worker outcome unknown after deadline; reconcile receipts before retry',
                verified_status=NULL,
                input_tokens=CASE WHEN a.runtime_context ? 'usage' THEN a.input_tokens ELSE NULL END,
                output_tokens=CASE WHEN a.runtime_context ? 'usage' THEN a.output_tokens ELSE NULL END,
                total_cost_usd=CASE WHEN a.runtime_context ? 'usage' THEN a.total_cost_usd ELSE NULL END,
                runtime_context=a.runtime_context || jsonb_build_object(
                    'verified',false,'unresolved',true,'recovery_required',true,
                    'usage',COALESCE(a.runtime_context->'usage',%s::jsonb))
                FROM expired e WHERE a.id=e.id AND a.tenant_id=%s RETURNING a.id""",
            (tenant, tenant, tenant, unknown, tenant),
        )
        return [str(row[0]) for row in cur.fetchall()]
