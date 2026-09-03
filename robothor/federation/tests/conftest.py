"""Two real instances on disk, and a capture of what each one persists.

Pairing is a two-party protocol, so testing it needs two identities with two
separate config directories. `saved` captures `save_connection` rather than
mocking it away — the point of these tests is that the issuer writes a row, so
a fixture that swallowed the write would certify the bug.
"""

from __future__ import annotations

import pytest

from robothor.federation.config import FederationConfig
from robothor.federation.identity import init_identity


def _instance(tmp_path, name: str) -> FederationConfig:
    config_dir = tmp_path / name
    config_dir.mkdir(parents=True, exist_ok=True)
    config = FederationConfig(
        instance_name=name,
        public_endpoint=f"nats://{name}.invalid:4222",
        config_dir=config_dir,
        identity_file=config_dir / "identity.json",
    )
    init_identity(config, display_name=name)
    return config


@pytest.fixture(autouse=True)
def _allow_inert_rls(monkeypatch):
    """These tests pair connections; they do not test the deployment gate.

    Activation refuses when row-level security is inert, which it is under a
    test runner connecting as a superuser. The gate has its own tests in
    test_rls_gate.py, and the soak proves it fires against a real pairing —
    so it is switched off here deliberately and visibly, rather than being
    satisfied by accident.
    """
    monkeypatch.setenv("ROBOTHOR_FEDERATION_ALLOW_INERT_RLS", "1")


@pytest.fixture
def parent_config(tmp_path):
    return _instance(tmp_path, "parent")


@pytest.fixture
def child_config(tmp_path):
    return _instance(tmp_path, "child")


@pytest.fixture(autouse=True)
def saved(monkeypatch):
    """Capture connections written by the code under test, keyed by id.

    Autouse because `create_invite_token` and `consume_invite_token` now
    persist a row, which turned this package's unit tests into tests that
    need a live Postgres. They passed on a workstation that has one and
    failed the CI unit lane, which has none. Capturing here keeps the whole
    package DB-free; the tests that care about the write still request
    `saved` by name and assert against it.
    """

    class _Store(dict):
        def get(self, connection_id):  # type: ignore[override]
            return dict.get(self, connection_id)

    store = _Store()

    def _capture(conn):
        store[conn.id] = conn

    monkeypatch.setattr("robothor.federation.identity.save_connection", _capture, raising=False)
    monkeypatch.setattr("robothor.federation.connections.save_connection", _capture)
    return store
