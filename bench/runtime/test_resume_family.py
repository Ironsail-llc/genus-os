"""Real resume selection on the disposable canonical migration database."""

import os
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.daemon import _resume_scan

pytestmark = pytest.mark.integration


def test_resume_family_scope_cycles_and_ordinary_work():
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires the disposable canonical migration harness")
    tenant, foreign = "resume-" + uuid4().hex, "resume-" + uuid4().hex
    root, child, grandchild, ordinary, foreign_root, cross_link = [str(uuid4()) for _ in range(6)]
    identifiers = [root, child, grandchild, ordinary, foreign_root, cross_link]
    try:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            for name in (tenant, foreign):
                cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (name, name))
            for identifier in identifiers:
                scope = foreign if identifier == foreign_root else tenant
                detail = "goal:synthetic" if identifier in {root, foreign_root} else "delegated"
                cur.execute(
                    "INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,trigger_detail,status,error_message,runtime_context) VALUES (%s,%s,'main','event',%s,'cancelled','daemon_restart','{}')",
                    (identifier, scope, detail),
                )
                cur.execute(
                    "INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version) VALUES (%s,1,'[]',1)",
                    (identifier,),
                )
            for identifier, parent in [
                (child, root),
                (grandchild, child),
                (root, grandchild),
                (cross_link, foreign_root),
            ]:
                cur.execute(
                    "UPDATE agent_runs SET parent_run_id=%s WHERE id=%s", (parent, identifier)
                )
        candidates = _resume_scan(tenant)
        assert {c.run_id for c in candidates} == {ordinary, cross_link}
        assert all(c.has_checkpoint and c.tenant_id == tenant for c in candidates)
    finally:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_runs WHERE id=ANY(%s::uuid[])", (identifiers,))
            cur.execute("DELETE FROM crm_tenants WHERE id=ANY(%s)", ([tenant, foreign],))


@pytest.mark.asyncio
@pytest.mark.parametrize("same_tenant", [True, False])
async def test_checkpoint_setup_keeps_plan_mode_within_its_tenant(same_tenant):
    from psycopg2.extras import Json

    from robothor.engine.models import TriggerType
    from robothor.engine.runtime.setup import restored_context
    from robothor.engine.task_context import install_context, make_context

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires the disposable canonical migration harness")
    owner, requester = "checkpoint-" + uuid4().hex, "checkpoint-" + uuid4().hex
    run = str(uuid4())
    messages = [{"role": "system", "content": "Synthetic checkpoint"}]
    install_context(messages, make_context("Prepare a draft", [], mode="plan"))
    try:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            for tenant in (owner, requester):
                cur.execute(
                    "INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant)
                )
            cur.execute(
                "INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status) VALUES (%s,%s,'main','event','cancelled')",
                (run, owner),
            )
            cur.execute(
                "INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version) VALUES (%s,1,%s,1)",
                (run, Json(messages)),
            )
        restored = await restored_context(
            run,
            "main",
            owner if same_tenant else requester,
            TriggerType.EVENT,
            False,
            True,
            None,
            "",
            "service",
        )
        assert restored[:2] == ((True, False) if same_tenant else (False, True))
        assert restored[2:] == (None, "", "service")
    finally:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_runs WHERE id=%s", (run,))
            cur.execute("DELETE FROM crm_tenants WHERE id=ANY(%s)", ([owner, requester],))
