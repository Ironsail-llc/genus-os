"""Prevent a status page's scripts from repeating a commitment during recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page, Route, WebSocketRoute


async def restrict_reconciliation(page: Page) -> None:
    # The worker creates a fresh context with service workers disabled. Refuse
    # reused contexts whose scripts or sockets could already be running.
    if page.url != "about:blank" or len(page.context.pages) != 1 or page.context.service_workers:
        raise PermissionError("fresh_reconciliation_context_required")

    async def read_only(route: Route) -> None:
        if route.request.method not in {"GET", "HEAD"}:
            await route.abort()
        else:
            await route.fallback()

    async def close_socket(route: WebSocketRoute) -> None:
        await route.close(code=1008)

    # Cover both this page and any new tabs, retaining the worker's public-
    # destination network checks through fallback rather than bypassing them.
    await page.context.route("**/*", read_only)
    await page.route("**/*", read_only)
    await page.context.route_web_socket("**/*", close_socket)
    await page.route_web_socket("**/*", close_socket)
