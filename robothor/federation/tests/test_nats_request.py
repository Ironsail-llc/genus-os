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


async def test_serve_requests_replies_with_handler_output():
    """serve_requests wires an inbound request to the handler and publishes the
    handler's reply back on the msg's reply subject."""
    mgr = NATSManager()
    captured = {}

    async def _subscribe(subject, cb=None):
        captured["cb"] = cb
        captured["subject"] = subject

    published = {}

    async def _publish(subject, data):
        published["subject"] = subject
        published["data"] = data

    mgr._nc = SimpleNamespace(subscribe=AsyncMock(side_effect=_subscribe), publish=_publish)

    async def _handler(data: bytes) -> bytes:
        assert data == b'{"op":"list_runs"}'
        return b'{"runs": [1, 2]}'

    ok = await mgr.serve_requests("conn-1", _handler)
    assert ok is True
    # Simulate an inbound request on the subscribed subject.
    msg = SimpleNamespace(data=b'{"op":"list_runs"}', reply="reply.subject")
    await captured["cb"](msg)
    assert published["subject"] == "reply.subject"
    assert published["data"] == b'{"runs": [1, 2]}'


async def test_serve_requests_handler_error_replies_error():
    mgr = NATSManager()
    captured = {}

    async def _subscribe(subject, cb=None):
        captured["cb"] = cb

    published = {}

    async def _publish(subject, data):
        published["data"] = data

    mgr._nc = SimpleNamespace(subscribe=AsyncMock(side_effect=_subscribe), publish=_publish)

    async def _boom(data: bytes) -> bytes:
        raise RuntimeError("handler exploded")

    await mgr.serve_requests("conn-1", _boom)
    await captured["cb"](SimpleNamespace(data=b"x", reply="r"))
    assert b"error" in published["data"]  # a failure still replies, doesn't hang the peer


async def test_serve_requests_false_when_not_connected():
    mgr = NATSManager()
    mgr._nc = None
    assert await mgr.serve_requests("conn-1", lambda d: d) is False
