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
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin, urlsplit

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


#: A material document is parsed into Chromium before anything can reject it,
#: so the transfer itself is bounded. The text taken out of it is capped at
#: 200_000 characters, and 2 MiB of markup carries that much text with room for
#: ordinary boilerplate. Without this bound a 40 MB response spent 20.6 s of
#: the 30 s collection budget in collect_material_documents, twice per
#: operation, before the text cap could refuse it.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
#: Redirects are followed here, not by Chromium: a fulfilled response's
#: redirect is not routed again, so Chromium would fetch the target with no
#: destination or size check at all. Only same-origin hops are followed --
#: the page keeps the requested URL, and the origin re-check after the read
#: must not be answered by a different site's document.
MAX_DOCUMENT_REDIRECTS = 3


async def public_destination(url: str) -> bool:
    """A public, resolvable HTTP(S) destination and nothing else.

    Deployments must additionally enforce broker network egress at the container
    boundary to close DNS rebinding between this resolution and Chromium's.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return False
    addresses = await asyncio.get_running_loop().getaddrinfo(
        parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
    )
    return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)


async def public_request(route: Route) -> None:
    """Reject non-public destinations on every request, including redirects."""
    try:
        if await public_destination(route.request.url):
            await route.continue_()
        else:
            await route.abort()
    except Exception:
        await route.abort()


async def public_document_request(route: Route) -> None:
    """A public destination, and a document small enough to be worth parsing."""
    try:
        url = route.request.url
        # The reading context carries no applicant state; keep it that way by
        # forwarding only what identifies the request as an ordinary read.
        headers = {
            name: value
            for name, value in route.request.headers.items()
            if name.lower() in {"accept", "accept-language", "user-agent"}
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            for _ in range(MAX_DOCUMENT_REDIRECTS + 1):
                if not await public_destination(url):
                    break
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, response.headers.get("location", ""))
                        if _same_origin(target, url):
                            url = target
                            continue
                        break
                    declared = response.headers.get("content-length")
                    if declared is not None and (
                        not declared.isdigit() or int(declared) > MAX_DOCUMENT_BYTES
                    ):
                        break
                    body = bytearray()
                    oversized = False
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DOCUMENT_BYTES:
                            oversized = True
                            break
                    if oversized:
                        break
                    # httpx has already decoded any content encoding, so the
                    # response's own transfer headers no longer describe these
                    # bytes. Only the media type survives.
                    await route.fulfill(
                        status=response.status_code,
                        headers={"content-type": response.headers.get("content-type", "")},
                        body=bytes(body),
                    )
                    return
        await route.abort()
    except Exception:
        await route.abort()


def _same_origin(target: str, source: str) -> bool:
    left, right = urlsplit(target), urlsplit(source)
    return (left.scheme, left.hostname, left.port) == (right.scheme, right.hostname, right.port)


async def handle(data: dict[str, Any]) -> dict[str, Any]:
    scope = Scope.model_validate(data["scope"])
    store = AutonomyStore(
        lambda: psycopg2.connect(**data["database"], connect_timeout=5),
        keys={key_id: base64.b64decode(value) for key_id, value in data["keys"].items()},
        key_id=data["key_id"],
    )
    settings = await asyncio.to_thread(store.settings, scope)
    reconcile = bool(data.get("reconcile"))
    # The broker re-reads the switch itself. It is a separate process started
    # from a payload, so it cannot take the parent's word for authority.
    if not settings.enabled:
        return {"error": "autonomous_execution_not_enabled"}
    row = await asyncio.to_thread(store.operation, scope, data["operation_id"])
    if (
        row["proposal"]["action"] in {"purchase", "subscription"}
        and not settings.payment_processing
    ):
        return {"error": "payment_processing_not_enabled"}
    if row["agent_id"] != data["agent_id"]:
        raise PermissionError("agent_not_allowed")
    if reconcile:
        await asyncio.to_thread(
            store.check_reconcile_authority, scope, data["operation_id"], data["agent_id"]
        )
        grant = None
    else:
        grant = await asyncio.to_thread(
            store.check_authority, scope, data["operation_id"], data["agent_id"]
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
        # A restored session is spent by the attempt that uses it. Left
        # reusable, one owner-requested check becomes an indefinite right to
        # reopen their authenticated merchant account after a restart.
        storage = await asyncio.to_thread(
            partial(store.consume_resource, one_shot=reconcile),
            scope,
            session_ref,
            destination,
            kind="browser_session",
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
