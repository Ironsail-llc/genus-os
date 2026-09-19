"""Observe one click in the retained page, including explicit server rejection."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from robothor.autonomy.broker import url_origin
from robothor.autonomy.confirmation import observe
from robothor.autonomy.inspection import inspect_page
from robothor.autonomy.workflows.rejection import RejectionWatch
from robothor.autonomy.workflows.transition import signature

if TYPE_CHECKING:
    from playwright.async_api import Page

    from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
    from robothor.autonomy.models import WebOperation

OUTCOME_TIMEOUT_SECONDS = 30


async def submit_and_observe(
    broker: BrowserBroker,
    page: Page,
    proposal: WebOperation,
    plan: ExecutionPlan,
    allowed_frames: frozenset[str],
    *,
    advance: bool,
) -> dict[str, Any]:
    before = (
        signature(
            await inspect_page(page, destination=proposal.origin, allowed_frames=allowed_frames)
        )
        if advance
        else set()
    )
    # Only zero-money form operations may recover from merchant field rejection.
    # Payment/code outcomes retain the existing reconciliation requirement.
    watch = None
    if proposal.action in {"account", "login", "application"} and not plan.challenge:
        watch = await RejectionWatch.create(page, plan)
    try:
        if watch:
            watch.start()
        await (await broker._unique(page.locator(plan.submit_selector))).click(timeout=15000)
        deadline = asyncio.get_running_loop().time() + OUTCOME_TIMEOUT_SECONDS
        while True:
            if url_origin(page.url) != proposal.origin:
                raise PermissionError("confirmation_origin_changed")
            if plan.success_selector:
                confirmation = page.locator(plan.success_selector)
                if await confirmation.is_visible():
                    text = await (await broker._unique(confirmation)).inner_text()
                    if plan.success_text and plan.success_text in text:
                        return {
                            "kind": "confirmation",
                            "evidence": {
                                "confirmation_sha256": hashlib.sha256(text.encode()).hexdigest()
                            },
                        }
            else:
                confirmed = await observe(page, proposal.origin, proposal.action)
                if confirmed:
                    digest, rule = next(iter(confirmed.items()))
                    return {
                        "kind": "confirmation",
                        "evidence": {"confirmation_sha256": digest, "confirmation_rule": rule},
                    }
            if watch:
                errors = await watch.rejected_fields()
                if errors:
                    return {"kind": "validation", "fields": errors}
            if advance:
                inspection = await inspect_page(
                    page, destination=proposal.origin, allowed_frames=allowed_frames
                )
                after = signature(inspection)
                if before - after and after - before:
                    return {"kind": "step", "inspection": inspection}
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("workflow_outcome_unknown")
            await asyncio.sleep(0.2)
    finally:
        if watch:
            await watch.close()
