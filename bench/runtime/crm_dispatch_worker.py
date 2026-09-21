"""Isolated native CRM dispatch timing worker; source provided by the host drill."""

import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import psycopg2

from robothor.crm import dal
from robothor.engine import permissions
from robothor.engine.tools import dispatch
from robothor.events import bus


async def main():
    assert Path(dispatch.__file__).resolve().is_relative_to(Path.cwd())
    dsn = os.environ["ROBOTHOR_TEST_DB_DSN"]
    assert "host=/tmp/runtime-migrated-" in dsn
    tenant = "dispatch-screen-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    permissions.check_tool_permission = lambda role, scope, name, **kw: (
        None if (role, scope, name) == ("service", tenant, "create_task") else "Denied"
    )
    dal._safe_audit = lambda *a, **k: None
    bus.publish = lambda *a, **k: None
    try:
        from robothor.engine.runtime import contracts
        from robothor.engine.runtime.current import active_context

        context_type = contracts.ExecutionContext
    except ImportError:
        context_type = active_context = None
    samples = []
    for index in range(31):
        token = None
        if active_context is not None:
            token = active_context.set(context_type(tenant, "service:main", str(uuid4())))
        started = time.perf_counter()
        try:
            result = await dispatch._execute_tool(
                "create_task",
                {"title": f"Synthetic sample {index}", "body": "Local screening"},
                agent_id="main",
                run_id=str(uuid4()),
                tenant_id=tenant,
                user_id="service:main",
                user_role="service",
            )
            elapsed = (time.perf_counter() - started) * 1000
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT title FROM crm_tasks WHERE tenant_id=%s AND id=%s",
                    (tenant, result.get("id")),
                )
                row = cur.fetchone()
                assert row == (f"Synthetic sample {index}",) and not result.get("error"), result
            sample = {
                "index": index,
                "warmup": index == 0,
                "duration_ms": elapsed,
                "verified": True,
            }
        except Exception as exc:
            sample = {
                "index": index,
                "warmup": index == 0,
                "duration_ms": (time.perf_counter() - started) * 1000,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        finally:
            if token is not None:
                active_context.reset(token)
        samples.append(sample)
        print("DISPATCH_SAMPLE " + json.dumps(sample), flush=True)
    assert all(sample["verified"] for sample in samples), "Preserved dispatch failures"
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (31,)


if __name__ == "__main__":
    asyncio.run(main())
