"""Best-effort private receipt observations after durable payment confirmation."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any, Literal

from robothor.autonomy.broker import url_origin
from robothor.autonomy.confirmation import observe
from robothor.autonomy.terms_audit import TermsAudit, TermsDocument, TermsSnapshot

if TYPE_CHECKING:
    from playwright.async_api import Page

    from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
    from robothor.autonomy.models import Scope


async def _witness(
    broker: BrowserBroker,
    page: Page,
    op: dict[str, Any],
    plan: ExecutionPlan | None,
    retained_selector: str | None,
) -> bool:
    digest = op["evidence"]["confirmation_sha256"]
    if not isinstance(digest, str):
        return False
    if retained_selector:
        from robothor.autonomy.workflows.retained import public_message

        text = await (await broker._unique(page.locator(retained_selector))).inner_text()
        masked = public_message(
            {"selector": retained_selector, "text": text}, broker.protected_values
        )
        return hashlib.sha256(masked["text"].encode()).hexdigest() == digest
    if plan and plan.success_selector:
        text = await (await broker._unique(page.locator(plan.success_selector))).inner_text()
        return broker._confirmation_digest(text, plan) == digest
    return digest in await observe(page, op["proposal"]["origin"], op["proposal"]["action"])


async def _page_document(
    broker: BrowserBroker,
    page: Page,
    op: dict[str, Any],
    plan: ExecutionPlan | None,
    retained_selector: str | None,
) -> tuple[TermsDocument, int]:
    destination = op["proposal"]["origin"]
    if url_origin(page.url) != destination or not await _witness(
        broker, page, op, plan, retained_selector
    ):
        raise ValueError("receipt_confirmation_changed")
    raw = await page.evaluate("() => (document.body?.innerText || '').slice(0,200001)")
    if url_origin(page.url) != destination or not await _witness(
        broker, page, op, plan, retained_selector
    ):
        raise ValueError("receipt_confirmation_changed")
    masked = broker.protected_values.text(raw).encode()
    text = masked[:100_000].decode(errors="ignore")
    return TermsDocument(
        origin=destination, text=text, text_truncated=len(raw) > 200_000 or len(masked) > 100_000
    ), max(0, len(page.frames) - 1)


async def capture_receipt(
    broker: BrowserBroker,
    scope: Scope,
    operation_id: str,
    agent_id: str,
    page: Page,
    *,
    plan: ExecutionPlan | None = None,
    retained_selector: str | None = None,
) -> dict[str, Any] | None:
    # Completion is already durable. Never propagate capture/storage failures
    # into a caller's submission retry handling or log exception/page contents.
    try:
        async with asyncio.timeout(5):
            op = await asyncio.to_thread(broker.store.operation, scope, operation_id)
            if op["proposal"]["action"] not in {"purchase", "subscription"}:
                return None
            if op["agent_id"] != agent_id or op["state"] != "completed":
                raise PermissionError("receipt_not_authorized")
            status: Literal["captured", "withheld_after_code", "unavailable"] = "captured"
            document = TermsDocument(origin=op["proposal"]["origin"], text="")
            omitted = 0
            if broker._used_transient_code:
                # Do not even inspect the DOM/storage: arbitrary code encodings
                # cannot be made safe merely by masking known strings.
                status = "withheld_after_code"
            else:
                try:
                    document, omitted = await _page_document(
                        broker, page, op, plan, retained_selector
                    )
                except Exception:
                    status = "unavailable"
            snapshot = TermsSnapshot(
                origin=op["proposal"]["origin"],
                phase="after_confirmation",
                confirmation_sha256=op["evidence"]["confirmation_sha256"],
                documents=[document],
                capture_status=status,
                omitted_frames=omitted,
            )
            result = await asyncio.to_thread(
                TermsAudit(broker.store).record, scope, operation_id, agent_id, snapshot
            )
            return {**result, "capture_status": status}
    except Exception:
        return {"capture_status": "unavailable"}
