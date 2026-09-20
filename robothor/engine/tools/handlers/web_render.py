"""Isolated JavaScript rendering of public pages with no interactive browser authority."""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from robothor.engine.web_render_network import RenderError, RenderNetwork, public_url
from robothor.settings import get_settings


class RenderRoutes:
    def __init__(self, network: Any, initial: Any = None) -> None:
        self.network = network
        self.initial = initial
        self.page: Any = None
        self.blocked = 0

    async def handle(self, route: Any) -> None:
        request = route.request
        if request.method != "GET" or request.resource_type not in {
            "document",
            "script",
            "stylesheet",
            "fetch",
            "xhr",
        }:
            self.blocked += 1
            await route.abort()
            return
        try:
            if request.resource_type == "document":
                # Only the supplied top-level document. No popups, iframe
                # browsing or script-driven navigation to another target.
                if (
                    self.initial is None
                    or request.url != self.initial["url"]
                    or request.frame.page != self.page
                    or request.frame.parent_frame is not None
                ):
                    raise RenderError("Unrequested document navigation")
                result = self.initial
            else:
                result = await self.network.get(request.url)
            await route.fulfill(
                status=result["status"], headers=result["headers"], body=result["body"]
            )
        except (RenderError, OSError, ValueError):
            self.blocked += 1
            await route.abort()
        except Exception:
            # A failed optional resource is incomplete coverage, not permission
            # to fall back to direct browser networking.
            self.blocked += 1
            await route.abort()


@asynccontextmanager
async def rejecting_proxy() -> AsyncIterator[str]:
    """Backstop any browser networking that route interception does not cover."""

    def reject(reader: Any, writer: Any) -> None:
        writer.close()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def render(url: str) -> dict[str, Any]:
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright

    network = RenderNetwork()
    initial = await network.get(url)
    if not 200 <= initial["status"] < 300:
        raise RenderError("Public document did not return a successful status")
    if "text/html" not in initial["headers"].get("content-type", "").lower():
        raise RenderError("Rendering requires an HTML document")
    routes = RenderRoutes(network, initial)
    with tempfile.TemporaryDirectory(prefix="genus-web-render-") as home:
        env: dict[str, str | float | bool] = {
            "HOME": home,
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
        }
        helper = get_settings().engine.web_render_sandbox_helper
        if helper:
            env["CHROME_DEVEL_SANDBOX"] = helper
        async with rejecting_proxy() as proxy, async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                env=env,
                proxy={"server": proxy, "bypass": "<-loopback>"},
                args=[
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
                timeout=10000,
            )
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                await context.route("**/*", routes.handle)

                async def close_socket(socket: Any) -> None:
                    await socket.close()

                await context.route_web_socket("**/*", close_socket)
                page = await context.new_page()
                routes.page = page
                await page.goto(initial["url"], wait_until="domcontentloaded", timeout=15000)
                with suppress(PlaywrightTimeout):
                    await page.wait_for_load_state("networkidle", timeout=5000)
                # Fixed extraction only; neither the model nor page supplies code.
                result = await page.evaluate("""() => ({
                    content: (document.body?.innerText || '').slice(0, 8000),
                    title: document.title.slice(0, 300),
                    links: Array.from(document.querySelectorAll('a[href]')).slice(0, 50)
                        .map(a => ({url: a.href, text: (a.innerText || '').slice(0, 200)}))
                })""")
                if not result["content"].strip() or not public_url(page.url):
                    raise RenderError("Rendered document has no usable text")
                result.update(
                    url=page.url,
                    status=initial["status"],
                    rendered=True,
                    requests=network.requests,
                    bytes=network.bytes,
                    blocked_resources=routes.blocked,
                )
                rendered: dict[str, Any] = result
                return rendered
            finally:
                await browser.close()


async def web_render(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    url = args.get("url")
    if set(args) != {"url"} or not isinstance(url, str) or not public_url(url):
        return {"error": "web_render accepts only a public HTTP(S) URL without credentials"}
    try:
        async with asyncio.timeout(35):
            return await render(url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Browser launch failures can include full process environments and
        # paths. Return only the class; do not expose raw Chromium diagnostics.
        return {"error": f"Public page rendering failed ({type(exc).__name__})"}


HANDLERS = {"web_render": web_render}
