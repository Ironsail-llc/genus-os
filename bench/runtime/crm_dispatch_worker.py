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


def install_profile():
    """Instrument selected boundaries; timings are inclusive and may overlap."""
    from functools import wraps

    from robothor.engine.tools import read_only

    stages = []

    def observe(module, name, label):
        original = getattr(module, name, None)
        if original is None:
            return
        if asyncio.iscoroutinefunction(original):

            @wraps(original)
            async def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return await original(*args, **kwargs)
                finally:
                    stages.append({"stage": label, "ms": (time.perf_counter() - started) * 1000})
        else:

            @wraps(original)
            def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    stages.append({"stage": label, "ms": (time.perf_counter() - started) * 1000})

        setattr(module, name, wrapped)

    observe(dispatch, "_runtime_denial", "runtime_denial")
    observe(dispatch, "_dispatch_admitted", "business_handler")
    observe(read_only, "declared_read_only_tools", "read_only_classification")
    try:
        from robothor.engine.runtime import effects, task_recovery
    except ImportError:
        pass
    else:
        for name in ("begin", "mark_dispatched", "finish", "read", "resolve"):
            observe(effects, name, "effect_" + name)
        observe(task_recovery, "verify", "crm_readback")
        observe(task_recovery, "recover", "receipt_recovery")
    return stages


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
    stages = install_profile() if os.environ.get("ROBOTHOR_RUNTIME_PROFILE_DISPATCH") else None
    plan = [("current", index == 0) for index in range(31)]
    reference = os.environ.get("ROBOTHOR_RUNTIME_EFFECT_REFERENCE")
    if reference:
        import random
        from types import ModuleType

        from robothor.engine.runtime import effect_dispatch

        prior = ModuleType("dispatch_reference")
        exec(compile(reference, "<trusted-git-dispatch-reference>", "exec"), prior.__dict__)
        current_invoke = effect_dispatch.invoke

        async def compared(*args, **kwargs):
            invoke = prior.invoke if variant == "reference" else current_invoke
            return await invoke(*args, **kwargs)

        effect_dispatch.invoke = compared
        trials = [(name, False) for name in ("reference", "current") for _ in range(30)]
        random.Random(142).shuffle(trials)
        plan = [("reference", True), ("current", True), *trials]
    samples = []
    for index, (variant, warmup) in enumerate(plan):
        if stages is not None:
            stages.clear()
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
                "warmup": warmup,
                "implementation": variant,
                "duration_ms": elapsed,
                "verified": True,
            }
        except Exception as exc:
            sample = {
                "index": index,
                "warmup": warmup,
                "implementation": variant,
                "duration_ms": (time.perf_counter() - started) * 1000,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        finally:
            if token is not None:
                active_context.reset(token)
        if stages is not None:
            sample["inclusive_stages"] = list(stages)
        samples.append(sample)
        print("DISPATCH_SAMPLE " + json.dumps(sample), flush=True)
    assert all(sample["verified"] for sample in samples), "Preserved dispatch failures"
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (len(plan),)


if __name__ == "__main__":
    asyncio.run(main())
