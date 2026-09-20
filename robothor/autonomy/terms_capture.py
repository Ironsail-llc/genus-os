"""Private rendered-text capture in the exclusive browser; never after code entry."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from robothor.autonomy.terms_audit import TermsAudit, TermsDocument, TermsSnapshot

if TYPE_CHECKING:
    from playwright.async_api import Page

    from robothor.autonomy.broker import BrowserBroker
    from robothor.autonomy.material_documents import MaterialTermTarget
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
    material_terms: list[MaterialTermTarget] | None = None,
    # Never ``None`` now: a refused observation is recorded rather than skipped,
    # so "no row for this phase" no longer means "we did not look".
) -> dict[str, Any]:
    from robothor.autonomy.broker import url_origin

    # A merchant can reflect a code using arbitrary transformations. Encryption
    # and string masking do not justify retaining that page after code entry.
    if broker._used_transient_code:
        if material_terms and phase == "before_input":
            from robothor.autonomy.material_documents import MaterialTermsUnavailableError

            raise MaterialTermsUnavailableError("material_terms_unavailable")
        # Suppressing the CONTENT is correct; suppressing the RECORD is not.
        # Card payments and emailed/TOTP sign-ups always reach this branch, so
        # without a row every one of them would show an empty audit trail that
        # reads exactly like the observation feature being switched off. Record
        # a zero-text row instead: the page is still never read here.
        suppressed = TermsSnapshot(
            origin=destination,
            phase=phase,
            documents=[TermsDocument(origin=destination, text="")],
            coverage="suppressed_after_code",
        )
        return await asyncio.to_thread(
            TermsAudit(broker.store).record, scope, operation_id, agent_id, suppressed
        )
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
    omitted_frames = max(0, len(page.frames) - len(documents))
    if material_terms:
        from robothor.autonomy.material_documents import collect_material_documents

        policy = await asyncio.to_thread(
            broker.store.check_authority, scope, operation_id, agent_id
        )
        documents.extend(
            await collect_material_documents(
                broker, page, material_terms, policy, destination, allowed_frames
            )
        )
    snapshot = TermsSnapshot(
        origin=destination,
        phase=phase,
        documents=documents,
        omitted_frames=omitted_frames,
        coverage="visible_text_and_selected_documents" if material_terms else "visible_text_only",
    )
    return await asyncio.to_thread(
        TermsAudit(broker.store).record, scope, operation_id, agent_id, snapshot
    )
