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
