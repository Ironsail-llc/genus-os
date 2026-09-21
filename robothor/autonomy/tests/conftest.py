from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from robothor.autonomy.models import RuntimeSettings, Scope
from robothor.autonomy.store import AutonomyStore


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return bool(cur.fetchone()[0])


@pytest.fixture(scope="session")
def migrated_dsn():
    """Apply the autonomy migrations ONCE per session, not once per test.

    Replaying them per test was harmless only while every migration was
    idempotent against existing rows. Migration 134 re-adds the narrow
    ``coverage`` CHECK that 142 widens, so a replay after a
    ``suppressed_after_code`` row exists fails constraint validation. Applying
    the chain once, before any test writes, is both correct and much faster;
    the cleanup below makes a reused (rather than freshly created) test
    database safe as well.
    """
    import os

    dsn = os.environ.get("AUTONOMY_TEST_DSN")
    if not dsn:
        pytest.skip("AUTONOMY_TEST_DSN must name a dedicated *_test database")
    conn = psycopg2.connect(dsn)
    assert conn.info.dbname.endswith("_test")
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM autonomy_terms_snapshots WHERE coverage NOT IN "
                "('visible_text_only','visible_text_and_selected_documents')"
                if _table_exists(cur, "autonomy_terms_snapshots")
                else "SELECT 1"
            )
            cur.execute(Path("crm/migrations/127_autonomous_execution.sql").read_text())
            cur.execute(Path("crm/migrations/128_autonomy_resource_descriptors.sql").read_text())
            cur.execute(Path("crm/migrations/129_autonomy_workflows.sql").read_text())
            cur.execute(Path("crm/migrations/130_autonomy_request_context.sql").read_text())
            cur.execute(Path("crm/migrations/131_autonomy_enrollments.sql").read_text())
            cur.execute(Path("crm/migrations/132_autonomy_enrollment_rls.sql").read_text())
            cur.execute(Path("crm/migrations/133_autonomy_terms_audit.sql").read_text())
            cur.execute(Path("crm/migrations/134_autonomy_material_documents.sql").read_text())
            cur.execute(Path("crm/migrations/138_autonomy_payment_events.sql").read_text())
            cur.execute(Path("crm/migrations/139_autonomy_receipt_observations.sql").read_text())
            cur.execute(Path("crm/migrations/140_autonomy_external_handoffs.sql").read_text())
            cur.execute(Path("crm/migrations/141_autonomy_handoff_check_leases.sql").read_text())
            cur.execute(Path("crm/migrations/142_autonomy_suppressed_terms.sql").read_text())
            cur.execute(Path("crm/migrations/143_autonomy_retention.sql").read_text())
    conn.close()
    return dsn


@pytest.fixture
def store(identity, migrated_dsn):
    dsn = migrated_dsn
    store = AutonomyStore(lambda: psycopg2.connect(dsn), keys={"v1": b"x" * 32}, key_id="v1")
    store.configure(
        identity,
        RuntimeSettings(
            enabled=True, payment_processing=True, payment_assessment_reference="synthetic-test"
        ),
    )
    return store


@pytest.fixture
def identity():
    return Scope(tenant_id="test-" + uuid4().hex, owner_id="alice")


@pytest.fixture(autouse=True)
def fresh_autonomy_settings():
    from robothor.settings import reset_settings

    reset_settings()
    yield
    reset_settings()
