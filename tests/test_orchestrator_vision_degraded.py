"""The vision proxy must say when the vision service is down.

Every read-only vision proxy route used to swallow the error and answer
"not available" — no log line, and the orchestrator's /health never mentioned
vision at all. So a crashed vision service and a box that never had one looked
the same to the operator. These tests pin the two signals: a log record naming
the route, and a `vision` component in /health that reports the last failure
(without flipping the overall status: vision is optional).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from robothor.api import orchestrator

_LOGGER = "robothor.api.orchestrator"


class _Client:
    """Stands in for httpx.AsyncClient; every request fails or succeeds as told."""

    def __init__(self, *, fail: bool, payload: dict[str, Any] | None = None) -> None:
        self._fail = fail
        self._payload = payload or {}

    def __call__(self, *args: Any, **kwargs: Any) -> _Client:
        return self

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def _respond(self, url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if self._fail:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=self._payload, request=request)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._respond(url)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._respond(url)


@pytest.fixture(autouse=True)
def _fresh_vision_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # raising=False only so the state starts clean whether or not it exists yet.
    monkeypatch.setattr(
        orchestrator, "_vision_state", {"available": None, "last_error": None}, raising=False
    )


def _stub_health_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _yes(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr("robothor.llm.ollama.check_model_available", _yes)
    monkeypatch.setattr("robothor.rag.reranker.check_reranker_available", _yes)
    monkeypatch.setattr("robothor.rag.web_search.check_searxng_available", _yes)


async def _call(route: str) -> Any:
    handlers = {
        "/vision/detect": orchestrator.vision_detect,
        "/vision/identify": orchestrator.vision_identify,
        "/vision/status": orchestrator.vision_status,
        "/vision/mode": orchestrator.vision_mode_get,
        "/vision/enrolled": orchestrator.vision_enrolled,
    }
    try:
        return await handlers[route]()
    except HTTPException as exc:
        return exc


@pytest.mark.parametrize(
    ("route", "upstream"),
    [
        ("/vision/detect", "/detections"),
        ("/vision/identify", "/identifications"),
        ("/vision/status", "/health"),
        ("/vision/mode", "/mode"),
        ("/vision/enrolled", "/enrolled"),
    ],
)
def test_an_unreachable_vision_service_is_logged_per_route(
    route: str, upstream: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", _Client(fail=True))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        result = asyncio.run(_call(route))

    # The fallback each route answered with is unchanged.
    if route in ("/vision/detect", "/vision/identify"):
        assert isinstance(result, HTTPException) and result.status_code == 503
    else:
        assert isinstance(result, dict) and not isinstance(result, HTTPException)

    records = [r for r in caplog.records if r.name == _LOGGER and upstream in r.getMessage()]
    assert records, f"no log record named {upstream} when {route} failed"
    assert "ConnectError" in records[0].getMessage()


def test_health_reports_vision_down_without_degrading_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_health_probes(monkeypatch)
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", _Client(fail=True))
    asyncio.run(_call("/vision/status"))

    body = asyncio.run(orchestrator.health())

    vision = body["optional"]["vision"]
    assert vision["available"] is False
    assert vision["last_error"] and "ConnectError" in vision["last_error"]
    assert body["status"] == "ok", "vision is optional; it must not degrade the orchestrator"
    # The dashboard reads every `components` entry with available=false as a
    # failing check and shows the orchestrator Degraded. Vision is reported
    # beside them, never among them.
    assert "vision" not in body["components"]
    assert all(c.get("available") is not False for c in body["components"].values())


def test_health_clears_the_vision_error_after_a_good_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_health_probes(monkeypatch)
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", _Client(fail=True))
    asyncio.run(_call("/vision/mode"))
    monkeypatch.setattr(
        orchestrator.httpx, "AsyncClient", _Client(fail=False, payload={"mode": "basic"})
    )
    asyncio.run(_call("/vision/mode"))

    vision = asyncio.run(orchestrator.health())["optional"]["vision"]
    assert vision == {"available": True, "last_error": None}


def test_health_before_any_vision_call_says_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_health_probes(monkeypatch)
    vision = asyncio.run(orchestrator.health())["optional"]["vision"]
    assert vision == {"available": None, "last_error": None}


def test_a_failed_hardening_step_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom() -> bool:
        raise OSError("no /proc here")

    monkeypatch.setattr("robothor.engine.process_hardening.harden_process", _boom)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        orchestrator._harden_at_import()
    assert any(
        "hardening" in r.getMessage().lower() and "no /proc here" in r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER
    )
