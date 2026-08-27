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


@pytest.fixture
def parent_config(tmp_path):
    return _instance(tmp_path, "parent")


@pytest.fixture
def child_config(tmp_path):
    return _instance(tmp_path, "child")


@pytest.fixture
def saved(monkeypatch):
    """Capture connections written by the code under test, keyed by id."""

    class _Store(dict):
        def get(self, connection_id):  # type: ignore[override]
            return dict.get(self, connection_id)

    store = _Store()

    def _capture(conn):
        store[conn.id] = conn

    monkeypatch.setattr("robothor.federation.identity.save_connection", _capture, raising=False)
    monkeypatch.setattr("robothor.federation.connections.save_connection", _capture)
    return store
