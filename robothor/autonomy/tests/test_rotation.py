"""Rotation survives a process restart and preserves late writes from old workers."""

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

from robothor.autonomy.models import ResourceInput
from robothor.autonomy.store import AutonomyStore


def test_persisted_key_rotation_and_old_worker_readability(monkeypatch, identity):
    dsn = os.environ.get("AUTONOMY_TEST_DSN")
    if not dsn:
        pytest.skip("dedicated test database required")
    schema = "rotation_" + uuid4().hex
    conn = psycopg2.connect(dsn)
    assert conn.info.dbname.endswith("_test")
    with conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg2.connect(dsn, options=f"-c search_path={schema}")

    monkeypatch.setattr("robothor.vault.crypto.get_master_key", lambda: b"z" * 32)
    try:
        setup = connect()
        with setup, setup.cursor() as cur:
            cur.execute(Path("crm/migrations/127_autonomous_execution.sql").read_text())
        setup.close()
        store = AutonomyStore(connect)
        original_id, original_keys = store.resource_keyring()
        old_worker = AutonomyStore(connect, keys=original_keys, key_id=original_id)
        resource = ResourceInput(
            kind="credential",
            label="Website login",
            origin="https://shop.example",
            payload=json.dumps({"username": "alice", "password": "private"}),
        )
        ref = store.put_resource(identity, resource)
        assert store.rotate_resource_keyring() == 1
        restarted = AutonomyStore(connect)
        assert restarted.key_id != original_id
        assert (
            restarted.consume_resource(identity, ref["id"], "https://shop.example")["password"]
            == "private"
        )
        late = old_worker.put_resource(identity, resource)
        assert (
            restarted.consume_resource(identity, late["id"], "https://shop.example")["password"]
            == "private"
        )
        assert restarted.rotate_resource_keyring() == 2
        assert (
            restarted.consume_resource(identity, ref["id"], "https://shop.example")["password"]
            == "private"
        )
    finally:
        with conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.close()
