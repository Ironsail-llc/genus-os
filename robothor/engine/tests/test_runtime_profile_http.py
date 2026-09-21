"""Real local chat delivery and durable Stop while profile reads remain blocked."""

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
import uvicorn
from bench.interactive.statistics import summary
from fastapi import FastAPI

from robothor.engine import chat
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.tests.test_runtime_admission_audit import (  # noqa: F401
    audit_store,
    identity,
    private_database,
    records,
)
from robothor.engine.tests.test_runtime_controls import runtime_db  # noqa: F401
from robothor.engine.tests.test_runtime_http_acceptance import event
from robothor.identity import IdentityContext


@pytest.mark.timeout(60)
async def test_chat_ack_progress_stop_and_audit_during_profile_lookup(
    request,
    audit_store,  # noqa: F811
    runtime_db,  # noqa: F811
    sample_agent_config,
    monkeypatch,
    tmp_path,  # noqa: F811
):
    engine = request.getfixturevalue("runner")
    auth = identity()
    sample_agent_config.difficulty_class = "simple"
    sessions = [chat.ChatSession()]
    gates = []

    def blocked(*args):
        entered, release, finished = gates[-1]
        entered.set()
        try:
            assert release.wait(30), "fixture lookup must be released"
            return sample_agent_config, ""
        finally:
            finished.set()

    loader = Mock(side_effect=blocked)
    model = AsyncMock(side_effect=AssertionError("Stopped preparation must not call a model"))
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    monkeypatch.setattr("litellm.acompletion", model)
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    monkeypatch.setattr(chat, "_get_session", lambda _: sessions[0])
    monkeypatch.setattr(chat, "save_exchange_async", AsyncMock())
    monkeypatch.setattr(
        chat,
        "_resolve_webchat_identity",
        lambda _: IdentityContext(auth.tenant_id, "webchat", auth.user_id, True, role="owner"),
    )
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    samples, progress_arrivals = [], []
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=15
        ) as client:
            for repetition in range(30):
                sessions[0] = chat.ChatSession()
                entered, release, finished = Event(), Event(), Event()
                gates.append((entered, release, finished))
                payload = {"message": "Synthetic preparation request", "request_id": str(uuid4())}
                started = time.perf_counter()
                async with client.stream("POST", "/chat/send", json=payload) as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    await asyncio.wait_for(event(lines, "accepted"), 2)
                    ack_ms = (time.perf_counter() - started) * 1000
                    assert await asyncio.to_thread(entered.wait, 1)
                    assert not finished.is_set()
                    if repetition == 0:
                        for _ in range(2):
                            update = await asyncio.wait_for(event(lines, "progress"), 10)
                            assert update["phase"] == "preparing"
                            assert "Preparing your request" in update["text"]
                            progress_arrivals.append((time.perf_counter() - started) * 1000)
                    stopping = time.perf_counter()
                    stopped = await client.post("/chat/abort", json=payload)
                    stop_ms = (time.perf_counter() - stopping) * 1000
                    assert stopped.status_code == 200 and stopped.json()["durable_stopped"]
                    await asyncio.wait_for(event(lines, "done"), 2)
                    result = await client.get(
                        "/chat/outcome", params={"request_id": payload["request_id"]}
                    )
                    assert result.status_code == 200
                    assert result.json()["terminal"] and result.json()["state"] == "cancelled"
                    assert "before execution began" in result.json()["text"]
                    assert not finished.is_set()
                    samples.append({"ack_ms": ack_ms, "stop_ack_ms": stop_ms})
                release.set()
                assert await asyncio.to_thread(finished.wait, 1)
        model.assert_not_awaited()
        engine.registry.execute.assert_not_called()
        assert loader.call_count == 30
    finally:
        for _, release, _ in gates:
            release.set()
        active = sessions[0].active_task
        if active and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()
        for _, _, finished in gates:
            assert await asyncio.to_thread(finished.wait, 1)
    report = {
        "scope": "Loopback TCP native chat, real private audit/control storage, blocked synthetic profile reads. No model or business calls.",
        "samples": samples,
        "ack_ms": summary([s["ack_ms"] for s in samples]),
        "stop_ack_ms": summary([s["stop_ack_ms"] for s in samples]),
        "progress_arrivals_ms": progress_arrivals,
    }
    output = Path(
        os.environ.get("ROBOTHOR_PROFILE_HTTP_OUTPUT", str(tmp_path / "profile-http.json"))
    )
    assert not output.exists(), "Preserve earlier evidence"
    output.write_text(json.dumps(report, indent=2) + "\n")
    assert report["ack_ms"]["p95"] < 2000 and report["stop_ack_ms"]["p95"] < 2000
    assert progress_arrivals[0] < 10000 and progress_arrivals[1] - progress_arrivals[0] < 10000
