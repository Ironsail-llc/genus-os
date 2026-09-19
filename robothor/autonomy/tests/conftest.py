from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from robothor.autonomy.models import RuntimeSettings, Scope
from robothor.autonomy.store import AutonomyStore


@pytest.fixture
def store(identity):
    import os

    dsn = os.environ.get("AUTONOMY_TEST_DSN")
    if not dsn:
        pytest.skip("AUTONOMY_TEST_DSN must name a dedicated *_test database")
    conn = psycopg2.connect(dsn)
    assert conn.info.dbname.endswith("_test")
    with conn:
        with conn.cursor() as cur:
            cur.execute(Path("crm/migrations/127_autonomous_execution.sql").read_text())
    conn.close()
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
