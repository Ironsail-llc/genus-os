"""Regression tests for bridge route execution models.

FastAPI runs normal ``def`` handlers in its worker threadpool.  Keeping routes
that call the synchronous CRM DAL, filesystem, or subprocess APIs out of the
event-loop thread prevents one slow operation from stalling every request.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from unittest.mock import patch

import pytest
from bridge_service import app
from fastapi.routing import APIRoute


def test_only_genuinely_async_routes_run_on_the_event_loop():
    # FastAPI 0.139 keeps included routers as lazy route groups instead of
    # flattening their APIRoutes into ``app.routes``.  Inspect the source
    # router when present while retaining compatibility with older FastAPI.
    routes = (
        candidate
        for outer_route in app.routes
        for candidate in getattr(
            getattr(outer_route, "original_router", None),
            "routes",
            (outer_route,),
        )
    )
    async_routes = {
        (method, route.path)
        for route in routes
        if isinstance(route, APIRoute) and inspect.iscoroutinefunction(route.endpoint)
        for method in route.methods
    }

    assert async_routes == {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/api/memory/entity/{name}"),
        ("POST", "/api/memory/search"),
        ("POST", "/api/memory/store"),
    }


@pytest.mark.asyncio
async def test_slow_crm_route_does_not_block_the_event_loop(test_client):
    entered = threading.Event()
    release = threading.Event()

    def slow_list_conversations(*args, **kwargs):
        entered.set()
        release.wait(timeout=1.0)
        return []

    # A timer makes the test self-releasing even if a regression runs the DAL
    # call on the event loop. In that case the sleep below resumes only after
    # the timer, making the elapsed-time assertion fail deterministically.
    timer = threading.Timer(0.6, release.set)
    timer.start()
    try:
        with patch("routers.conversations.list_conversations", slow_list_conversations):
            request = asyncio.create_task(test_client.get("/api/conversations"))
            started_at = asyncio.get_running_loop().time()
            await asyncio.sleep(0.05)
            elapsed = asyncio.get_running_loop().time() - started_at

            assert entered.is_set()
            assert elapsed < 0.4

            release.set()
            response = await request
    finally:
        release.set()
        timer.cancel()

    assert response.status_code == 200
    assert response.json() == []
