"""Explicitly selected public contract documents, read without applicant state."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import Field, field_validator

from robothor.autonomy.models import StrictModel, origin
from robothor.autonomy.terms_audit import TermsDocument

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page, Route

    from robothor.autonomy.broker import BrowserBroker
    from robothor.autonomy.models import Delegation


class MaterialTermTarget(StrictModel):
    selector: str = Field(min_length=1, max_length=500)
    frame_selector: str | None = Field(default=None, max_length=500)
    frame_origin: str | None = None

    _frame_origin = field_validator("frame_origin")(lambda value: origin(value) if value else None)


class MaterialTermsUnavailableError(ValueError):
    """A fixed public reason; browser exceptions and document values stay private."""


def permitted(destination: str, policy: Delegation, operation_origin: str) -> bool:
    return policy.allow_any_website or destination in policy.origins | policy.frame_origins | {
        operation_origin
    }


async def _target_url(
    broker: BrowserBroker,
    page: Page,
    target: MaterialTermTarget,
    destination: str,
    allowed_frames: frozenset[str],
) -> str:
    from robothor.autonomy.broker import url_origin

    root: Page | Frame = page
    expected = destination
    if target.frame_selector:
        expected = target.frame_origin or ""
        if expected not in allowed_frames | {destination}:
            raise MaterialTermsUnavailableError("material_terms_unavailable")
        handle = await (await broker._unique(page.locator(target.frame_selector))).element_handle()
        frame = await handle.content_frame() if handle else None
        if not frame or url_origin(frame.url) != expected:
            raise MaterialTermsUnavailableError("material_terms_unavailable")
        await broker._guard_origin(page, frame, expected)
        root = frame
    locator = await broker._unique(root.locator(target.selector))
    if not await locator.is_visible():
        raise MaterialTermsUnavailableError("material_terms_unavailable")
    value = await locator.evaluate("(el) => ({tag:el.tagName, href:el.href})")
    if value["tag"] != "A" or not isinstance(value.get("href"), str) or len(value["href"]) > 2000:
        raise MaterialTermsUnavailableError("material_terms_unavailable")
    if (
        url_origin(root.url) != expected
        or broker.protected_values.text(value["href"]) != value["href"]
    ):
        raise MaterialTermsUnavailableError("material_terms_unavailable")
    return str(value["href"])


async def _read(
    broker: BrowserBroker, page: Page, url: str, policy: Delegation, destination: str
) -> TermsDocument:
    from robothor.autonomy.broker import url_origin
    from robothor.autonomy.worker import public_request

    if not permitted(url_origin(url), policy, destination):
        raise MaterialTermsUnavailableError("material_terms_unavailable")
    browser = page.context.browser
    if browser is None:
        raise MaterialTermsUnavailableError("material_terms_unavailable")
    context = await browser.new_context(
        java_script_enabled=False, accept_downloads=False, service_workers="block"
    )

    async def route_document(route: Route) -> None:
        try:
            request = route.request
            allowed = (
                request.method == "GET"
                and request.resource_type == "document"
                and request.frame == document.main_frame
                and len(request.url) <= 2000
                and broker.protected_values.text(request.url) == request.url
                and permitted(url_origin(request.url), policy, destination)
            )
            if allowed:
                await (broker.public_document_router or public_request)(route)
            else:
                await route.abort()
        except Exception:
            await route.abort()

    try:
        document = await context.new_page()
        await context.route("**/*", route_document)
        response = await document.goto(url, wait_until="domcontentloaded", timeout=20000)
        if not response or not response.ok:
            raise MaterialTermsUnavailableError("material_terms_unavailable")
        media = (await response.header_value("content-type") or "").split(";")[0].strip().lower()
        if media not in {"text/html", "text/plain", "application/xhtml+xml"}:
            raise MaterialTermsUnavailableError("material_terms_unavailable")
        source_url = document.url
        if (
            not permitted(url_origin(source_url), policy, destination)
            or broker.protected_values.text(source_url) != source_url
        ):
            raise MaterialTermsUnavailableError("material_terms_unavailable")
        value = await document.evaluate(
            "() => ({text:(document.body?.innerText || '').slice(0,200001),login:!!document.querySelector('input[type=password]')})"
        )
        if (
            value["login"]
            or not value["text"].strip()
            or len(value["text"]) > 200000
            or document.url != source_url
        ):
            raise MaterialTermsUnavailableError("material_terms_unavailable")
        return TermsDocument(
            origin=url_origin(source_url),
            text=broker.protected_values.text(value["text"]),
            source="linked_document",
            source_url=source_url,
            requested_url=url,
        )
    finally:
        await context.close()


async def collect_material_documents(
    broker: BrowserBroker,
    page: Page,
    targets: list[MaterialTermTarget],
    policy: Delegation,
    destination: str,
    allowed_frames: frozenset[str],
) -> list[TermsDocument]:
    documents = []
    try:
        async with asyncio.timeout(30):
            for target in targets:
                url = await _target_url(broker, page, target, destination, allowed_frames)
                documents.append(await _read(broker, page, url, policy, destination))
        return documents
    except Exception:
        raise MaterialTermsUnavailableError("material_terms_unavailable") from None
