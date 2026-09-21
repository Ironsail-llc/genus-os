"""Enrollment isolation is also enforced by PostgreSQL for a scoped non-superuser.

This test sets ``app.tenant_id`` itself, which is the right shape for checking
migration 132's POLICY — and for a long time it was the only place the GUC was
ever set, because ``AutonomyStore.transaction()`` never did. It therefore
certified a control the product had not switched on. ``test_store_rls.py`` is
the one that goes through the product's own connection; read it too.
"""

from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql


def test_enrollment_rls_filters_reads_and_rejects_foreign_writes(store, identity):
    assert store._connect is not None
    conn = store._connect()
    role = "intake_rls_" + uuid4().hex
    try:
        with conn.cursor() as cur:
            cur.execute(Path("crm/migrations/132_autonomy_enrollment_rls.sql").read_text())
            cur.execute(
                sql.SQL("CREATE ROLE {} NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role))
            )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT ON autonomy_enrollments TO {}").format(
                    sql.Identifier(role)
                )
            )
            for tenant in (identity.tenant_id, "foreign-" + uuid4().hex):
                cur.execute(
                    "INSERT INTO autonomy_enrollments(id,tenant_id,owner_id,token_hash,kind,expires_at) VALUES (%s,%s,'alice',%s,'profile',now()+interval '1 minute')",
                    (str(uuid4()), tenant, uuid4().hex),
                )
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            cur.execute("SELECT set_config('app.tenant_id',%s,true)", (identity.tenant_id,))
            cur.execute("SELECT tenant_id FROM autonomy_enrollments")
            assert cur.fetchall() == [(identity.tenant_id,)]
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO autonomy_enrollments(id,tenant_id,owner_id,token_hash,kind,expires_at) VALUES (%s,'foreign','alice',%s,'profile',now()+interval '1 minute')",
                    (str(uuid4()), uuid4().hex),
                )
    finally:
        conn.rollback()  # Includes the temporary role and all fixture rows.
        conn.close()
