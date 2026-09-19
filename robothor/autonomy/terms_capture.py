"""Private rendered-text capture in the exclusive browser; never after code entry."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from robothor.autonomy.terms_audit import TermsAudit, TermsDocument, TermsSnapshot

if TYPE_CHECKING:
    from playwright.async_api import Page

    from robothor.autonomy.broker import BrowserBroker
    from robothor.autonomy.models import Scope

# Bound extraction in the renderer, before transferring page text to Python.
_EXTRACT = """() => {
 const text=(document.body?.innerText || '').slice(0,200001);
 const anchors=Array.from(document.querySelectorAll('a[href]'))
   .filter(a => a.getClientRects().length && getComputedStyle(a).visibility !== 'hidden');
 return {text,links:anchors.slice(0,101).map(a=>a.href.slice(0,2001))};
}"""


async def capture_terms(
    broker: BrowserBroker,
    scope: Scope,
    operation_id: str,
    agent_id: str,
    page: Page,
    destination: str,
    allowed_frames: frozenset[str],
    *,
    phase: Literal["before_input", "before_submit"],
) -> dict[str, Any] | None:
    from robothor.autonomy.broker import url_origin

    # A merchant can reflect a code using arbitrary transformations. Encryption
    # and string masking do not justify retaining that page after code entry.
    if broker._used_transient_code:
        return None
    if url_origin(page.url) != destination:
        raise PermissionError("audit_origin_changed")
    documents = []
    text_budget, link_budget = 100_000, 25_000
    frames = [page.main_frame, *page.main_frame.child_frames]
    for frame in frames[:21]:
        try:
            frame_origin = url_origin(frame.url)
        except ValueError:
            continue
        if frame_origin not in allowed_frames | {destination}:
            continue
        await broker._guard_origin(page, frame, frame_origin)
        value = await frame.evaluate(_EXTRACT)
        if url_origin(frame.url) != frame_origin:
            raise PermissionError("audit_origin_changed")
        raw_text = broker.protected_values.text(value["text"]).encode()
        text = raw_text[:text_budget].decode(errors="ignore")
        text_budget -= len(text.encode())
        links = []
        for raw_link in value["links"][:100]:
            link = broker.protected_values.text(raw_link)
            size = len(link.encode())
            if len(link) > 2000 or size > link_budget:
                continue
            links.append(link)
            link_budget -= size
        documents.append(
            TermsDocument(
                origin=frame_origin,
                text=text,
                links=links,
                text_truncated=len(value["text"]) > 200000 or len(text.encode()) < len(raw_text),
                links_truncated=len(links) < len(value["links"]),
            )
        )
    snapshot = TermsSnapshot(
        origin=destination,
        phase=phase,
        documents=documents,
        omitted_frames=max(0, len(page.frames) - len(documents)),
    )
    return await asyncio.to_thread(
        TermsAudit(broker.store).record, scope, operation_id, agent_id, snapshot
    )
