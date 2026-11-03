"""An IDE run carries the signed-in person's identity.

The IDE authenticates its websocket with the same user token as webchat, but
started runs with no ``identity=``, so anything that reads the run's verified
identity (credential intake among them) treated every IDE run as a sender
nobody had verified.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Session:
    def __init__(self, auth):
        self.auth = auth
        self.session_id = "s1"
        self.history: list = []
        self.active_task = None
        self.sent: list = []

    async def send_result(self, request_id, result):
        self.sent.append(("result", result))

    async def send_error(self, request_id, code, message):
        self.sent.append(("error", message))

    async def send_notification(self, method, params):
        self.sent.append((method, params))


def _auth(typ: str = "user"):
    from robothor.auth.deps import AuthContext

    return AuthContext(user_id="acct-alice", tenant_id="default", role="owner", typ=typ)


async def _send(monkeypatch, auth, resolved):
    from robothor.engine import ide

    seen: dict = {}

    class _Runner:
        async def execute(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                output_text="",
                status=SimpleNamespace(value="completed"),
                input_tokens=0,
                output_tokens=0,
                total_cost_usd=0.0,
                id="run-1",
            )

    calls: list = []

    def resolve(channel, identifier, tenant_id, **kw):
        calls.append((channel, identifier, tenant_id))
        return resolved

    import robothor.identity as identity_module

    monkeypatch.setattr(identity_module, "resolve_identity", resolve)
    monkeypatch.setattr(ide, "_runner", _Runner())
    monkeypatch.setattr(ide, "_config", SimpleNamespace(default_chat_agent="main"))
    session = _Session(auth)
    await ide._handle_chat_send(session, 1, {"message": "hello"})
    await asyncio.wait_for(session.active_task, 5)
    return seen, calls


@pytest.mark.asyncio
async def test_an_ide_run_carries_the_signed_in_persons_identity(monkeypatch):
    marker = object()
    seen, calls = await _send(monkeypatch, _auth(), marker)
    assert calls == [("webchat", "acct-alice", "default")]
    assert seen.get("identity") is marker


@pytest.mark.asyncio
async def test_a_service_token_resolves_no_person(monkeypatch):
    seen, calls = await _send(monkeypatch, _auth("service"), object())
    assert calls == []
    assert seen.get("identity") is None


@pytest.mark.asyncio
async def test_an_ide_run_passes_what_the_person_typed(monkeypatch):
    seen, _calls = await _send(monkeypatch, _auth(), object())
    assert seen.get("typed_message") == "hello"
