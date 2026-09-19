"""One broker request per short-lived process. Stdout is a safe result only."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import shutil
import socket
import sys
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx
import psycopg2
from playwright.async_api import async_playwright
from pydantic import SecretStr

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan, url_origin
from robothor.autonomy.models import ResourceInput, Scope
from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserType, Route, StorageState


async def launch_local(chromium: BrowserType) -> Browser:
    # Distribution Chromium carries the host's AppArmor/user-namespace policy;
    # a downloaded executable may not. Always retain Chromium's own sandbox.
    from robothor.settings import get_settings

    executable = get_settings().autonomy.chromium_executable or shutil.which("chromium")
    return await chromium.launch(
        headless=True,
        chromium_sandbox=True,
        executable_path=executable,
        env={**browser_environment()},
    )


def browser_environment() -> dict[str, str]:
    """No provider/database secrets, debug flags, preload hooks or tracing."""
    from robothor.settings.env import process_env_allowlist

    return process_env_allowlist(("PATH", "HOME", "LANG", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH"))


async def public_request(route: Route) -> None:
    """Reject non-public destinations on every request, including redirects.

    Deployments must additionally enforce broker network egress at the container
    boundary to close DNS rebinding between this resolution and Chromium's.
    """
    try:
        parsed = urlsplit(route.request.url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            await route.abort()
            return
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            await route.abort()
            return
        await route.continue_()
    except Exception:
        await route.abort()


async def handle(data: dict[str, Any]) -> dict[str, Any]:
    scope = Scope.model_validate(data["scope"])
    store = AutonomyStore(
        lambda: psycopg2.connect(**data["database"], connect_timeout=5),
        keys={key_id: base64.b64decode(value) for key_id, value in data["keys"].items()},
        key_id=data["key_id"],
    )
    settings = await asyncio.to_thread(store.settings, scope)
    reconcile = bool(data.get("reconcile"))
    if not settings.enabled and not reconcile:
        return {"error": "autonomous_execution_not_enabled"}
    row = await asyncio.to_thread(store.operation, scope, data["operation_id"])
    if (
        row["proposal"]["action"] in {"purchase", "subscription"}
        and not settings.payment_processing
        and not reconcile
    ):
        return {"error": "payment_processing_not_enabled"}
    if row["agent_id"] != data["agent_id"]:
        raise PermissionError("agent_not_allowed")
    grant = (
        None
        if reconcile
        else await asyncio.to_thread(
            store.check_authority, scope, data["operation_id"], data["agent_id"]
        )
    )
    plan = ExecutionPlan.model_validate(data["plan"]) if data.get("plan") else None
    destination = row["proposal"]["origin"]
    url = data.get("inspect_url") or (plan.url if plan else "")
    if url_origin(url) != destination:
        return {"error": "destination_mismatch"}
    storage = None
    session_ref = data.get("session_resource_id") or (
        str(plan.session_resource_id) if plan and plan.session_resource_id else None
    )
    if session_ref:
        storage = await asyncio.to_thread(
            store.consume_resource, scope, session_ref, destination, kind="browser_session"
        )
    broker = BrowserBroker(store)
    broker.protected_values.session(storage)
    async with async_playwright() as pw:
        browser = None
        if data.get("managed"):
            if not settings.managed_browser or not data.get("browserbase_key"):
                return {"error": "managed_browser_not_configured"}
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.browserbase.com/v1/sessions",
                    headers={"X-BB-API-Key": data["browserbase_key"]},
                    json={
                        "timeout": 180,
                        "keepAlive": False,
                        "browserSettings": {
                            "recordSession": False,
                            "logSession": False,
                            "solveCaptchas": True,
                        },
                    },
                )
                response.raise_for_status()
                endpoint = response.json()["connectUrl"]
                browser = await pw.chromium.connect_over_cdp(endpoint)
        else:
            browser = await launch_local(pw.chromium)
        try:
            context = await browser.new_context(
                storage_state=cast("StorageState | None", storage),
                accept_downloads=False,
                service_workers="block",
            )
            await context.route("**/*", public_request)
            page = await context.new_page()
            if reconcile and plan:
                return await broker.reconcile_on_page(
                    scope, data["operation_id"], data["agent_id"], plan, page
                )
            if plan:
                assert grant is not None
                return await broker.execute_on_page(
                    scope,
                    data["operation_id"],
                    data["agent_id"],
                    plan,
                    page,
                    allowed_frames=grant.frame_origins,
                    verification_code=data.get("verification_code"),
                )
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if url_origin(page.url) != destination:
                return {"error": "destination_changed"}
            inspected = await broker.inspect(
                page, destination, grant.frame_origins if grant else frozenset()
            )
            saved = await asyncio.to_thread(
                store.put_resource,
                scope,
                ResourceInput(
                    kind="browser_session",
                    label="Website session",
                    origin=destination,
                    payload=SecretStr(json.dumps(await context.storage_state())),
                ),
                source="broker_session",
            )
            return {
                "origin": destination,
                **inspected,
                "session_resource_id": saved["id"],
            }
        finally:
            try:
                await browser.close()
            finally:
                broker.protected_values.clear()


def main() -> None:
    logging.disable(logging.CRITICAL)
    from robothor.engine.process_hardening import harden_process

    if not harden_process():
        sys.stdout.write(json.dumps({"error": "broker_process_isolation_unavailable"}))
        return
    data = None
    try:
        data = json.loads(sys.stdin.buffer.read(1_000_001))
        result = asyncio.run(handle(data))
        sys.stdout.write(json.dumps(result))
    except Exception:
        sys.stdout.write(json.dumps({"error": "broker_request_failed"}))
    finally:
        if isinstance(data, dict):
            data.clear()


if __name__ == "__main__":
    main()
