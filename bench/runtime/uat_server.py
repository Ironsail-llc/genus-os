"""Loopback-only acceptance workspace backed by a disposable PostgreSQL cluster.

Uses the real goal router and lifecycle store. No agent scheduler, model provider,
business connector, or production database is started by this harness.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate

ROOT = Path(__file__).resolve().parents[2]


def seed_unfinished_work(tenant, *, kind="short"):
    goal = store.create(
        tenant,
        CreateGoal(
            objective="Finish the requested work",
            kind=kind,
            success_criteria=["The requested work has been checked"],
        ),
        "uat-operator",
    )
    remaining = str(uuid4())
    for task_id, title, status in [
        (str(uuid4()), "Prepare the first item", "DONE"),
        (remaining, "Check the remaining item", "TODO"),
    ]:
        with store.transaction() as cur:
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,%s,%s)",
                (task_id, tenant, title, status),
            )
        goal = store.update(
            tenant,
            goal["id"],
            GoalUpdate(action="link_task", version=goal["version"], task_id=task_id),
            "uat-operator",
        )
    store.update(
        tenant,
        goal["id"],
        GoalUpdate(
            action="wait",
            version=goal["version"],
            task_id=remaining,
            note="One requested task still needs checking",
        ),
        "uat-agent",
    )


def seed(tenant):
    store.set_enabled(tenant, False, "uat-operator")
    seed_unfinished_work(tenant)
    parent = store.create(
        tenant,
        CreateGoal(
            objective="Prepare customer follow-up",
            kind="long",
            token_budget=2000,
            success_criteria=["Operator approves the draft", "Customer reply reviewed"],
        ),
        "uat-operator",
    )
    child = store.create(
        tenant,
        CreateGoal(
            objective="Review tomorrow's customer reply",
            parent_goal_id=parent["id"],
            success_criteria=["Reply reviewed"],
            human_review=True,
        ),
        "uat-operator",
    )
    store.update(
        tenant,
        child["id"],
        GoalUpdate(
            action="wait",
            version=child["version"],
            note="Waiting for a synthetic customer reply tomorrow",
            event_type="uat.customer_reply",
        ),
        "uat-agent",
    )
    parent = store.get(tenant, parent["id"])
    store.update(
        tenant,
        parent["id"],
        GoalUpdate(
            action="wait",
            version=parent["version"],
            note="Child is waiting for the customer",
        ),
        "uat-agent",
    )
    review = store.create(
        tenant,
        CreateGoal(
            objective="Review the synthetic draft",
            success_criteria=["Draft is suitable for the customer"],
            human_review=True,
        ),
        "uat-operator",
    )
    review = store.update(
        tenant,
        review["id"],
        GoalUpdate(
            action="evidence",
            version=review["version"],
            criterion=0,
            note="Synthetic draft is ready for your judgment",
            reference="uat:draft",
            satisfied=True,
        ),
        "uat-agent",
    )
    store.update(
        tenant,
        review["id"],
        GoalUpdate(
            action="complete",
            version=review["version"],
            note="Operator review required",
        ),
        "uat-agent",
    )


def application(dsn, ui_port=5321):
    @contextmanager
    def connection():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    store.get_connection = connection
    state = {"tenant": str(uuid4())}
    seed(state["tenant"])
    sys.path.insert(0, str(ROOT / "crm/bridge"))
    from routers.pursuit_goals import router

    app = FastAPI(title="Synthetic runtime acceptance workspace")

    @app.middleware("http")
    async def local_operator(request: Request, call_next):
        if request.client.host not in {"127.0.0.1", "::1"} or request.headers.get("origin") not in {
            None,
            f"http://127.0.0.1:{ui_port}",
            f"http://localhost:{ui_port}",
        }:
            return JSONResponse({"detail": "local acceptance workspace only"}, status_code=403)
        request.state.auth = SimpleNamespace(
            tenant_id=state["tenant"],
            role="owner",
            is_service=False,
            actor_id="uat-operator",
        )
        return await call_next(request)

    @app.post("/uat/reset")
    def reset():
        tenant = str(uuid4())
        seed(tenant)
        state["tenant"] = tenant
        return {"reset": True}

    @app.get("/uat/status")
    def status():
        return {"synthetic": True, "models_enabled": False, "tenant": state["tenant"]}

    app.include_router(router)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5322)
    parser.add_argument("--ui-port", type=int, default=5321)
    args = parser.parse_args()
    binary = next(
        p
        for p in (Path("/usr/lib/postgresql/18/bin"), Path("/usr/lib/postgresql/16/bin"))
        if (p / "initdb").exists()
    )
    with tempfile.TemporaryDirectory(prefix="robothor-uat-") as directory:
        root = Path(directory)
        data, socket = root / "data", root / "socket"
        socket.mkdir()

        def command(name, *args):
            subprocess.run([str(binary / name), *map(str, args)], check=True, capture_output=True)

        command("initdb", "-D", data, "-U", "uat", "--auth=trust", "--no-locale")
        command(
            "pg_ctl",
            "-D",
            data,
            "-l",
            root / "postgres.log",
            "-o",
            f"-F -h '' -k {socket}",
            "-w",
            "start",
        )
        try:
            command("createdb", "-h", socket, "-U", "uat", "runtime_uat_test")
            dsn = f"dbname=runtime_uat_test user=uat host={socket}"
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE crm_tasks(id UUID PRIMARY KEY,tenant_id TEXT NOT NULL,title TEXT,objective TEXT,status TEXT,resolution TEXT,deleted_at TIMESTAMPTZ,tags TEXT[],session_goal_meta JSONB)"
                )
                cur.execute(
                    "CREATE TABLE crm_agent_notifications(id UUID,tenant_id TEXT,from_agent TEXT,to_agent TEXT,notification_type TEXT,subject TEXT,body TEXT,metadata JSONB)"
                )
                cur.execute((ROOT / "crm/migrations/126_goal_pursuit.sql").read_text())
                cur.execute((ROOT / "crm/migrations/141_runtime_effects.sql").read_text())
                cur.execute((ROOT / "crm/migrations/142_goal_effect_lookup.sql").read_text())
            uvicorn.run(application(dsn, args.ui_port), host="127.0.0.1", port=args.port)
        finally:
            command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


if __name__ == "__main__":
    main()
