"""Recovery contexts cannot send mutable HTTP or websocket messages."""

import asyncio

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.readonly_browser import restrict_reconciliation

pytestmark = pytest.mark.e2e


async def test_reconciliation_blocks_websocket_before_server_connection():
    connections = []

    async def connected(reader, writer):
        connections.append(True)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route(
                "https://shop.example/status",
                lambda route: route.fulfill(content_type="text/html", body="<p>Status</p>"),
            )
            await restrict_reconciliation(page)
            await page.goto("https://shop.example/status")
            result = await page.evaluate(
                """url => new Promise(resolve => {
              const ws = new WebSocket(url);
              ws.onclose = () => resolve('closed');
              ws.onerror = () => resolve('error');
              setTimeout(() => resolve('timeout'), 1000);
            })""",
                f"ws://127.0.0.1:{port}/submit",
            )
            assert result in {"closed", "error"}
            assert connections == []
        finally:
            await browser.close()


async def test_reconciliation_refuses_previously_running_page_context():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto("data:text/html,<p>Existing context</p>")
            with pytest.raises(PermissionError, match="fresh_reconciliation_context_required"):
                await restrict_reconciliation(page)
        finally:
            await browser.close()
