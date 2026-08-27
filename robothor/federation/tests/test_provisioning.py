"""Pairing has to provision the broker, or the operator pairs into a wall.

`render_config` and `PeerAccount` had no production caller — only the soak,
which built the account config by hand. So `robothor federation invite` minted
a token, persisted a row, and created no NATS account at all: the peer would
redeem the token and then be refused by the broker with an Authorization
Violation, having done everything right.

That is the same built-wired-tested-inert shape as `set_nats_manager`, which
had no production caller either and left this instance unable to make an
outbound federation call for five months. The wiring ratchet now covers this
one too.

The whole accounts block is regenerated from the full connection list rather
than appended to. NATS declares an export on the EXPORTING account, so adding
one peer edits the ENGINE account as well as adding a new one — an append-only
scheme would drift the moment a connection was revoked.
"""

from __future__ import annotations

import pytest

from robothor.federation.models import Connection, ConnectionState, Relationship
from robothor.federation.nats_config import ENGINE_ACCOUNT, account_name
from robothor.federation.provisioning import (
    peer_credentials,
    provision_broker,
    reload_broker,
)


def _conn(cid, state=ConnectionState.ACTIVE, transport=None):
    return Connection(
        id=cid,
        peer_name=f"peer-{cid}",
        relationship=Relationship.CHILD,
        state=state,
        direction="inbound",
        transport=transport if transport is not None else {},
    )


@pytest.fixture
def broker_dir(tmp_path):
    main = tmp_path / "nats-server.conf"
    main.write_text("listen: 127.0.0.1:4222\n")
    return main


# ── Generating the accounts file ─────────────────────────────────────


def test_provisioning_writes_an_accounts_file(broker_dir):
    result = provision_broker([_conn("c1")], main_config=broker_dir, engine_password="pw")

    assert result.accounts_path.exists()
    assert account_name("c1") in result.accounts_path.read_text()


def test_the_main_config_gains_exactly_one_include(broker_dir):
    provision_broker([_conn("c1")], main_config=broker_dir, engine_password="pw")
    provision_broker([_conn("c1"), _conn("c2")], main_config=broker_dir, engine_password="pw")

    assert broker_dir.read_text().count("include") == 1, (
        "the include must be idempotent; a second invite duplicated it"
    )


def test_a_revoked_connection_disappears_from_the_config(broker_dir):
    provision_broker([_conn("c1"), _conn("c2")], main_config=broker_dir, engine_password="pw")
    result = provision_broker([_conn("c1")], main_config=broker_dir, engine_password="pw")

    text = result.accounts_path.read_text()
    assert account_name("c1") in text
    assert account_name("c2") not in text, (
        "an append-only scheme leaves a revoked peer's credential live"
    )


def test_a_pending_connection_is_still_provisioned(broker_dir):
    """The peer has to be able to CONNECT in order to complete the handshake
    that activates it. Provisioning only ACTIVE connections would deadlock
    every pairing."""
    result = provision_broker(
        [_conn("c1", state=ConnectionState.PENDING)], main_config=broker_dir, engine_password="pw"
    )

    assert account_name("c1") in result.accounts_path.read_text()


def test_a_suspended_connection_loses_its_account(broker_dir):
    """Suspension is the operator's kill switch. Leaving the broker account in
    place would let a suspended peer keep a live connection."""
    result = provision_broker(
        [_conn("c1", state=ConnectionState.SUSPENDED)], main_config=broker_dir, engine_password="pw"
    )

    assert account_name("c1") not in result.accounts_path.read_text()


def test_the_engine_account_is_always_present(broker_dir):
    result = provision_broker([], main_config=broker_dir, engine_password="pw")

    assert ENGINE_ACCOUNT in result.accounts_path.read_text()


# ── Credentials ──────────────────────────────────────────────────────


def test_a_connection_keeps_the_credential_it_was_given(broker_dir):
    """Regenerating the config must not rotate a live peer's password out from
    under it — every other connection would survive and that one would start
    failing authorization for no visible reason."""
    conn = _conn("c1")
    first = provision_broker([conn], main_config=broker_dir, engine_password="pw")
    password = first.peers[0].password

    again = provision_broker([conn], main_config=broker_dir, engine_password="pw")

    assert again.peers[0].password == password


def test_the_credential_is_stored_on_the_connection(broker_dir):
    conn = _conn("c1")
    provision_broker([conn], main_config=broker_dir, engine_password="pw")

    creds = peer_credentials(conn)
    assert creds["user"].startswith("fed_")
    assert len(creds["password"]) >= 24
    assert conn.transport["password"] == creds["password"]


def test_two_connections_never_share_a_credential(broker_dir):
    a, b = _conn("c1"), _conn("c2")
    provision_broker([a, b], main_config=broker_dir, engine_password="pw")

    assert peer_credentials(a)["password"] != peer_credentials(b)["password"]


def test_the_accounts_file_is_not_world_readable(broker_dir):
    """It holds every peer's password in plaintext, which is what nats-server
    requires. It must not be readable by anything else on the box."""
    result = provision_broker([_conn("c1")], main_config=broker_dir, engine_password="pw")

    assert result.accounts_path.stat().st_mode & 0o077 == 0


# ── Reload ───────────────────────────────────────────────────────────


def test_reload_reports_failure_rather_than_raising(monkeypatch):
    """A config the broker has not read is a config that does nothing. The
    caller has to be able to say so; it must not take the CLI down."""
    monkeypatch.setattr(
        "robothor.federation.provisioning._run",
        lambda *a, **k: (1, "Failed to reload robothor-nats.service"),
    )

    ok, detail = reload_broker()

    assert ok is False
    assert "reload" in detail.lower()


def test_reload_reports_success(monkeypatch):
    monkeypatch.setattr("robothor.federation.provisioning._run", lambda *a, **k: (0, ""))

    ok, _ = reload_broker()

    assert ok is True
