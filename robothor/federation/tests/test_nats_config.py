"""The generated broker config must make a child structurally unable to reach
anything it was not exported.

These assert on the rendered text; `test_transport_real_broker.py` proves a
real nats-server actually enforces what the text says. Neither alone is enough:
a config that reads correctly and a server that refuses correctly are different
claims, and this feature has a documented history of the first without the
second.
"""

from __future__ import annotations

import pytest

from robothor.federation.nats_config import (
    ENGINE_ACCOUNT,
    PeerAccount,
    account_name,
    render_config,
)


@pytest.fixture
def two_peers():
    return [
        PeerAccount("11111111-1111-1111-1111-111111111111", password="pw-a"),
        PeerAccount("22222222-2222-2222-2222-222222222222", password="pw-b"),
    ]


def test_each_peer_gets_its_own_account(two_peers):
    cfg = render_config(engine_password="engine-pw", peers=two_peers)
    for p in two_peers:
        assert f"{p.account}: {{" in cfg


def test_a_peer_imports_only_its_own_command_subject(two_peers):
    cfg = render_config(engine_password="engine-pw", peers=two_peers)
    a, b = two_peers

    account_a = cfg.split(f"{a.account}: {{")[1].split("  }")[0]
    assert a.subject in account_a
    assert b.subject not in account_a, "one child can address another child's subject"


def test_no_peer_account_is_granted_jetstream(two_peers):
    """An account with no `jetstream` key cannot address $JS.API.>. That is what
    keeps a compromised child away from the 1 GB message store."""
    cfg = render_config(engine_password="engine-pw", peers=two_peers, jetstream_dir="/var/lib/nats")

    for p in two_peers:
        block = cfg.split(f"{p.account}: {{")[1].split("  }")[0]
        assert "jetstream" not in block


def test_the_engine_account_exports_but_never_imports(two_peers):
    """The asymmetry is the absence of a line. If ENGINE imported a service
    from a peer account, the peer would have a subject on which to serve us —
    and a child would have a foothold in its parent."""
    cfg = render_config(engine_password="engine-pw", peers=two_peers)

    engine_block = cfg.split(f"{ENGINE_ACCOUNT}: {{")[1].split("\n  FED_")[0]
    assert "exports:" in engine_block
    assert "imports:" not in engine_block


def test_there_is_no_leafnode_block(two_peers):
    """`leafnodes { listen: 0.0.0.0:7422 }` with no authorization block bound
    every interface into the same global account as the engine, including
    $JS.API.>. Nothing uses leaf nodes."""
    cfg = render_config(engine_password="engine-pw", peers=two_peers)
    assert "leafnodes" not in cfg


def test_it_binds_loopback_by_default(two_peers):
    cfg = render_config(engine_password="engine-pw", peers=two_peers)
    assert "listen: 127.0.0.1:4222" in cfg


def test_an_instance_with_no_peers_exports_nothing():
    """A box that has never federated must not carry an empty exports block
    that someone later fills in by hand."""
    cfg = render_config(engine_password="engine-pw", peers=[])
    assert "exports:" not in cfg
    assert "FED_" not in cfg


def test_account_names_survive_a_uuid():
    assert account_name("ab-cd-ef") == "FED_AB_CD_EF"


def test_every_peer_gets_a_distinct_password():
    a = PeerAccount("11111111-1111-1111-1111-111111111111")
    b = PeerAccount("22222222-2222-2222-2222-222222222222")
    assert a.password != b.password
    assert len(a.password) >= 24


def test_a_peer_may_publish_only_to_its_own_subject(two_peers):
    """Account isolation makes a sibling's subject unroutable, which produces
    no evidence. An explicit allow-list makes the attempt a logged refusal."""
    cfg = render_config(engine_password="engine-pw", peers=two_peers)
    a, b = two_peers

    block = cfg.split(f"{a.account}: {{")[1].split("\n  }")[0]
    assert f'publish: {{ allow: ["{a.subject}"] }}' in block
    assert b.subject not in block


def test_a_peer_may_subscribe_only_to_its_own_inbox(two_peers):
    """Reply delivery needs _INBOX.>; nothing else does. A peer that could
    subscribe to `robothor.>` would see every connection's traffic without
    making a single request the application could audit."""
    cfg = render_config(engine_password="engine-pw", peers=two_peers)
    block = cfg.split(f"{two_peers[0].account}: {{")[1].split("\n  }")[0]

    assert 'subscribe: { allow: ["_INBOX.>"] }' in block
    assert "robothor.>" not in block
