"""Concurrent local HTTP Stop/audit while all profile-reader capacity is occupied."""

import asyncio
import json
import os
import random
import socket
import time
from pathlib import Path
from threading import Event, Lock
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from bench.interactive.statistics import quantile, summary
from robothor.engine import chat
from robothor.engine.tests.conftest import engine_config, sample_agent_config  # noqa: F401
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


def batch_summary(samples, metric):
    """Resample rounds together because callers within a round share load."""
    result = summary([sample[metric] for sample in samples])
    groups = {}
    for sample in samples:
        groups.setdefault(sample["repetition"], []).append(sample[metric])
    batches = list(groups.values())
    rng = random.Random(20260920)
    estimates = [
        quantile([value for batch in rng.choices(batches, k=len(batches)) for value in batch], 0.95)
        for _ in range(1000)
    ]
    result["p95_ci95"] = [quantile(estimates, 0.025), quantile(estimates, 0.975)]
    result["uncertainty"] = "1000 bootstrap resamples of whole concurrent rounds"
    return result


@pytest.mark.timeout(180)
@pytest.mark.parametrize("tenants", [1, 5, 20])
async def test_concurrent_profile_stop(
    request,
    audit_store,  # noqa: F811
    runtime_db,  # noqa: F811
    sample_agent_config,  # noqa: F811
    monkeypatch,
    tmp_path,
    tenants,
):
    engine = request.getfixturevalue("runner")
    identities = {str(i): identity() for i in range(tenants)}
    sessions, samples = {}, []
    release, lock = Event(), Lock()
    entered = 0

    def blocked(*args):
        nonlocal entered
        with lock:
            entered += 1
        assert release.wait(15), "fixture must release profile readers"
        return sample_agent_config, ""

    loader = Mock(side_effect=blocked)
    model = AsyncMock(side_effect=AssertionError("Stopped preparation cannot call a model"))
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    monkeypatch.setattr("litellm.acompletion", model)
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    monkeypatch.setattr(
        chat, "_get_session", lambda key: sessions.setdefault(key, chat.ChatSession())
    )
    monkeypatch.setattr(chat, "save_exchange_async", AsyncMock())
    monkeypatch.setattr(
        chat,
        "_resolve_webchat_identity",
        lambda auth: IdentityContext(auth.tenant_id, "webchat", auth.user_id, True, role="owner"),
    )
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = identities[request.headers["x-fixture-tenant"]]
        return await call_next(request)

    app.include_router(chat.router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    tasks = []
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=10
        ) as client:
            for repetition in range(30):
                release.clear()
                entered = 0
                accepted = []
                may_stop = asyncio.Event()

                async def exercise(
                    index, repetition=repetition, accepted=accepted, may_stop=may_stop
                ):
                    headers = {"x-fixture-tenant": str(index)}
                    payload = {
                        "message": "Synthetic blocked preparation",
                        "request_id": str(uuid4()),
                        "session_key": f"agent:main:load-{repetition}-{index}",
                    }
                    started = time.perf_counter()
                    async with client.stream(
                        "POST", "/chat/send", json=payload, headers=headers
                    ) as response:
                        assert response.status_code == 200
                        lines = response.aiter_lines()
                        await asyncio.wait_for(event(lines, "accepted"), 2)
                        ack_ms = (time.perf_counter() - started) * 1000
                        accepted.append(index)
                        await may_stop.wait()
                        stopping = time.perf_counter()
                        stopped = await client.post("/chat/abort", json=payload, headers=headers)
                        stop_ms = (time.perf_counter() - stopping) * 1000
                        assert stopped.status_code == 200 and stopped.json()["durable_stopped"]
                        await asyncio.wait_for(event(lines, "done"), 2)
                        outcome = await client.get(
                            "/chat/outcome",
                            headers=headers,
                            params={key: payload[key] for key in ("request_id", "session_key")},
                        )
                        assert outcome.status_code == 200
                        body = outcome.json()
                        assert body["terminal"] and body["state"] == "cancelled"
                        assert "before execution began" in body["text"]
                        assert not release.is_set()
                        return {
                            "repetition": repetition,
                            "tenant": index,
                            "ack_ms": ack_ms,
                            "stop_ack_ms": stop_ms,
                        }

                tasks = [asyncio.create_task(exercise(index)) for index in range(tenants)]
                async with asyncio.timeout(3):
                    while len(accepted) != tenants or entered != min(tenants, 4):
                        if any(task.done() for task in tasks):
                            await asyncio.gather(*tasks)
                        await asyncio.sleep(0.005)
                may_stop.set()
                samples.extend(await asyncio.gather(*tasks))
                # All callers are stopped before the actual readers finish.
                # Queued lookups must not execute when capacity is released.
                release.set()
                from robothor.engine.runtime.profile_pool import read_profile

                await read_profile(lambda: None)
                assert loader.call_count == (repetition + 1) * min(tenants, 4)
        model.assert_not_awaited()
        engine.registry.execute.assert_not_called()
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in sessions.values():
            if session.active_task and not session.active_task.done():
                session.active_task.cancel()
                await asyncio.gather(session.active_task, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()
    report = {
        "tenants": tenants,
        "repetitions": 30,
        "samples": samples,
        "ack_ms": batch_summary(samples, "ack_ms"),
        "stop_ack_ms": batch_summary(samples, "stop_ack_ms"),
        "profile_reads": loader.call_count,
        "model_calls": 0,
        "business_calls": 0,
        "scope": "Loopback HTTP, synthetic blocked profile reads, private audit/control tables. All requests stop and recover their own cancelled audit before releasing readers; queued lookups never execute.",
    }
    directory = Path(os.environ.get("ROBOTHOR_PROFILE_LOAD_OUTPUT", str(tmp_path)))
    output = directory / f"profile-load-{tenants}.json"
    assert not output.exists(), "Preserve earlier evidence"
    output.write_text(json.dumps(report, indent=2) + "\n")
    assert report["ack_ms"]["p95"] < 2000 and report["stop_ack_ms"]["p95"] < 2000
