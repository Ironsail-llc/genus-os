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
        # Personal enrollment awaits request bodies; all synchronous vault and
        # journal calls use asyncio.to_thread. Verification starts an async
        # broker task, so it must also retain the event-loop context.
        ("POST", "/api/autonomy/handoffs/{handoff_id}/check"),
        ("GET", "/api/autonomy/status"),
        ("GET", "/api/autonomy/operations"),
        ("GET", "/api/autonomy/operations/{operation_id}/payment"),
        ("GET", "/api/autonomy/operations/{operation_id}/terms"),
        ("GET", "/api/autonomy/operations/{operation_id}/terms/{snapshot_id}"),
        ("POST", "/api/autonomy/resources"),
        ("POST", "/api/autonomy/enrollments"),
        ("POST", "/api/autonomy/enrollments/inspect"),
        ("POST", "/api/autonomy/enrollments/complete"),
        ("POST", "/api/autonomy/resources/refresh-descriptions"),
        ("POST", "/api/autonomy/profile-from-contact"),
        ("DELETE", "/api/autonomy/resources/{resource_id}"),
        ("POST", "/api/autonomy/grants"),
        ("DELETE", "/api/autonomy/grants/{grant_id}"),
        ("DELETE", "/api/autonomy/grants/{grant_id}/payment-hold"),
        ("PUT", "/api/autonomy/settings"),
        ("POST", "/api/autonomy/operations/{operation_id}/verification"),
        # The owner's route out of an operation no check can ever resolve.
        ("POST", "/api/autonomy/operations/{operation_id}/abandon"),
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/api/memory/entity/{name}"),
        ("POST", "/api/memory/search"),
        ("POST", "/api/memory/store"),
        # The provider routes await the engine over HTTP — that is the whole
        # of what they do, so the event loop is where they belong. The
        # blocking parts they still own (the psycopg2 vault write, the
        # _defaults.yaml read-modify-write) go through asyncio.to_thread
        # inside the handler rather than making the route synchronous.
        ("GET", "/api/providers"),
        ("GET", "/api/models"),
        ("PUT", "/api/providers/{provider_id}/keys/{position}"),
        ("DELETE", "/api/providers/{provider_id}/keys/{position}"),
        ("POST", "/api/providers/{provider_id}/test"),
        ("GET", "/api/providers/defaults"),
        ("PATCH", "/api/providers/defaults"),
        # The three channel-access MUTATIONS await the engine after the write:
        # the binding is a row this process owns, but the engine's belief about
        # who a sender is lives in its own process for up to 300s, so a revoke
        # that did not reach it would leave the revoked sender running. The
        # psycopg2 half goes through asyncio.to_thread inside each handler. The
        # two READS stay synchronous -- they await nothing.
        ("POST", "/api/channels/{name}/pairings/{code}/approve"),
        ("POST", "/api/channels/{name}/pairings/{code}/deny"),
        ("DELETE", "/api/channels/{name}/identities/{identity_id}"),
        # The two channel-status routes await the engine over HTTP, and that is
        # the whole of what they do: a channel is an object in the ENGINE
        # process holding that process's credentials, so nothing here can ask
        # one whether it is configured or make it prove it works. The listing's
        # own blocking half — the access mode and the pending-code count — goes
        # through a single asyncio.to_thread for the whole set rather than one
        # hop per channel.
        ("GET", "/api/channels"),
        ("POST", "/api/channels/{name}/verify"),
        # The four plugin routes await the engine over HTTP and do nothing
        # else. A plugin is an object in the ENGINE process — what loaded,
        # what was refused, and which discovery generation the caches are
        # serving are facts only that process holds — so this side owns no
        # blocking work at all: it does not read the lockfile, and a second
        # implementation of "which plugins are installed" here would answer
        # from a different view of site-packages.
        ("GET", "/api/plugins"),
        ("POST", "/api/plugins/sync"),
        ("POST", "/api/plugins/reload"),
        ("POST", "/api/plugins/{name}/enable"),
        ("POST", "/api/plugins/{name}/disable"),
        # Install and remove await the engine too. The pip subprocess and the
        # lockfile write happen in the ENGINE's threadpool, behind its own
        # asyncio.to_thread and a 60s cap; this side is one HTTP call.
        ("POST", "/api/plugins/install"),
        ("POST", "/api/plugins/{name}/remove"),
        # The doctor route awaits asyncio.to_thread and nothing else. Every
        # check underneath it is synchronous -- psycopg2, urllib, a subprocess
        # -- and run_sync opens its own event loop, which it cannot do on the
        # one serving the request. Async here is what keeps the whole run OFF
        # the loop; a def route would block it for the length of the report.
        ("GET", "/api/doctor"),
        # First-run setup. Each of these awaits something genuinely
        # asynchronous -- the engine over HTTP, api.telegram.org, or several
        # asyncio.to_thread probes gathered in parallel -- and every blocking
        # piece they own (the vault write, the argon2 hash, the account
        # INSERT, the template install, the doctor run) is handed to
        # asyncio.to_thread inside the handler.
        #
        # POST /api/setup/claim is the deliberate exception and is NOT here: it
        # is a ``def`` handler for the same reason the sign-in routes are, so
        # the file read and the constant-time compare run in the worker
        # threadpool rather than on the loop that serves every other request.
        ("GET", "/api/setup/status"),
        ("GET", "/api/setup/detect"),
        ("POST", "/api/setup/operator"),
        ("POST", "/api/setup/provider"),
        ("POST", "/api/setup/channel"),
        ("POST", "/api/setup/agent"),
        ("POST", "/api/setup/complete"),
        # The agent-manifest routes await the engine over HTTP — the tool list
        # for validation, the scheduler reconcile after every write, the manual
        # trigger — and that is the bulk of what they do. Every blocking piece
        # they own (the manifest-directory scan, the schema validation, the
        # snapshot, the atomic write) goes through asyncio.to_thread inside the
        # handler rather than making the route synchronous, because a `def`
        # route cannot await the engine at all and a write that does not
        # reconcile is a write that changes nothing.
        ("GET", "/api/agent-manifests"),
        ("GET", "/api/agent-manifests/{agent_id}"),
        ("POST", "/api/agent-manifests/validate"),
        ("POST", "/api/agent-manifests"),
        ("PATCH", "/api/agent-manifests/{agent_id}"),
        ("POST", "/api/agent-manifests/{agent_id}/enable"),
        ("POST", "/api/agent-manifests/{agent_id}/disable"),
        ("DELETE", "/api/agent-manifests/{agent_id}"),
        ("POST", "/api/agent-manifests/{agent_id}/run"),
        # The three install-state mutations. Each awaits the engine reconcile —
        # without it the manifest the installer just wrote does not fire until
        # the next restart — and hands its own blocking work (the hub download,
        # the template installer, the removal) to asyncio.to_thread. The
        # read-only listing and readiness routes stay `def`.
        ("POST", "/api/installed-agents/install"),
        ("POST", "/api/installed-agents/{agent_id}/update"),
        ("DELETE", "/api/installed-agents/{agent_id}"),
        # The two export routes await nothing, and are still `async def`: both
        # hand ALL of their work — a template render, a tarball, a full-text
        # scan of every file — to asyncio.to_thread, so the thin coroutine that
        # remains is the right shape. Written `def` they would occupy a worker
        # thread for the whole export instead of releasing it.
        ("POST", "/api/installed-agents/{agent_id}/export"),
        ("GET", "/api/installed-agents/{agent_id}/export/plan"),
        # Answering a waiting question. One of its three kinds — a permission
        # escalation — is an asyncio.Event inside the ENGINE process, so the
        # route awaits engine_request and a `def` handler could not settle it at
        # all. The two durable kinds are psycopg2 writes and go through
        # asyncio.to_thread inside the handler. The read-only listing beside it
        # stays `def` and is correctly absent from this set.
        ("POST", "/api/approvals/{kind}/{approval_id}"),
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
