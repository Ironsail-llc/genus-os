"""NATS request-reply transport for federation (Wave-2, W2-9)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from robothor.federation import nats as nats_mod
from robothor.federation.nats import NATSManager


async def test_request_returns_reply_bytes():
    mgr = NATSManager()
    reply = SimpleNamespace(data=b'{"runs": []}')
    mgr._nc = SimpleNamespace(request=AsyncMock(return_value=reply))
    out = await mgr.request("conn-1", b'{"op":"list_runs"}', timeout=1.0)
    assert out == b'{"runs": []}'


async def test_request_none_when_not_connected():
    mgr = NATSManager()
    mgr._nc = None
    assert await mgr.request("conn-1", b"x") is None


async def test_request_none_on_error():
    mgr = NATSManager()
    mgr._nc = SimpleNamespace(request=AsyncMock(side_effect=TimeoutError()))
    assert await mgr.request("conn-1", b"x", timeout=0.1) is None


def test_singleton_set_get():
    mgr = NATSManager()
    nats_mod.set_nats_manager(mgr)
    assert nats_mod.get_nats_manager() is mgr
    nats_mod.set_nats_manager(None)
    assert nats_mod.get_nats_manager() is None
