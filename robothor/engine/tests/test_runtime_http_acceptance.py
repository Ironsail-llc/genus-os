"""Local TCP delivery through the native chat runner and durable stop store."""

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from bench.interactive.statistics import summary
from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.tests.test_runtime_controls import runtime_db  # noqa: F401
from robothor.goals.tests.test_store import private_database  # noqa: F401
from robothor.identity import IdentityContext


async def event(lines, wanted):
    kind, data = "", ""
    async for line in lines:
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: "):
            data += line[6:]
        elif not line:
            if kind == wanted:
                return json.loads(data)
            kind, data = "", ""
    raise AssertionError(f"Stream ended before {wanted}")


@pytest.mark.timeout(60)
@pytest.mark.usefixtures("_mock_run_persistence")
async def test_native_http_acceptance_progress_and_durable_stop(
    request,
    runtime_db,  # noqa: F811
    sample_agent_config,
    monkeypatch,
    tmp_path,
):
    import litellm

    engine = request.getfixturevalue("runner")
    sample_agent_config.task_protocol = False
    monkeypatch.setattr(engine, "_persist_run_sync", MagicMock())
    monkeypatch.setattr(
        "robothor.engine.runner.load_agent_config_or_reason",
        lambda *args, **kwargs: (sample_agent_config, None),
    )
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    monkeypatch.setattr(chat, "save_exchange_async", AsyncMock())
    sessions = [chat.ChatSession()]
    monkeypatch.setattr(chat, "_get_session", lambda key: sessions[0])
    auth = AuthContext(user_id="owner", tenant_id=engine.config.tenant_id, role="owner", typ="user")
    identity = IdentityContext(auth.tenant_id, "webchat", "owner", True, role="owner")
    monkeypatch.setattr(chat, "_resolve_webchat_identity", lambda _: identity)
    entered = asyncio.Event()
    cancelled = []

    async def provider(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(litellm, "acompletion", provider)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    samples, progress_arrivals = [], []
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
            for repetition in range(30):
                sessions[0] = chat.ChatSession()
                entered.clear()
                payload = {"message": "Synthetic waiting request", "request_id": str(uuid4())}
                started = time.perf_counter()
                async with client.stream("POST", "/chat/send", json=payload) as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    await asyncio.wait_for(event(lines, "accepted"), 2)
                    ack = (time.perf_counter() - started) * 1000
                    await asyncio.wait_for(entered.wait(), 2)
                    if repetition == 0:
                        for _ in range(2):
                            progress = await asyncio.wait_for(event(lines, "progress"), 12)
                            progress_arrivals.append((time.perf_counter() - started) * 1000)
                            assert progress["text"]
                    stopping = time.perf_counter()
                    stopped = await client.post("/chat/abort", json=payload)
                    stop_ack = (time.perf_counter() - stopping) * 1000
                    assert stopped.status_code == 200
                    assert stopped.json()["durable_stopped"] is True
                    done = await asyncio.wait_for(event(lines, "done"), 2)
                    assert done.get("status") == "cancelled" or done.get("aborted") is True
                    assert len(cancelled) == repetition + 1
                    samples.append({"ack_ms": ack, "stop_ack_ms": stop_ack})
        with runtime_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agent_runtime_request_stops WHERE tenant_id=%s",
                (auth.tenant_id,),
            )
            assert cur.fetchone()[0] == 30
    finally:
        active = sessions[0].active_task
        if active and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()
    report = {
        "scope": "Loopback TCP, real native chat runner, private PostgreSQL; blocked synthetic provider, no business effects. Not a deployment ingress or load certification.",
        "samples": samples,
        "ack_ms": summary([s["ack_ms"] for s in samples]),
        "stop_ack_ms": summary([s["stop_ack_ms"] for s in samples]),
        "progress_arrivals_ms": progress_arrivals,
        "progress_interval_ms": progress_arrivals[1] - progress_arrivals[0],
    }
    output = Path(os.environ.get("ROBOTHOR_RUNTIME_HTTP_OUTPUT", str(tmp_path / "http.json")))
    output.write_text(json.dumps(report, indent=2) + "\n")
    assert report["ack_ms"]["p95"] < 2000
    assert report["stop_ack_ms"]["p95"] < 2000
    # Allow scheduling/transport overhead around the ten-second emitter interval.
    assert progress_arrivals[0] < 11000
    assert report["progress_interval_ms"] < 11000
