"""Observe a form-step transition without treating it as final submission."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from robothor.autonomy.confirmation import observe
from robothor.autonomy.inspection import inspect_page

if TYPE_CHECKING:
    from playwright.async_api import Page


def signature(inspection: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (field.get("frame_origin", ""), field.get("frame_selector", ""), field["selector"])
        for field in inspection["fields"]
        if field["tag"] in {"input", "select", "textarea"}
        and field.get("type") not in {"button", "submit", "reset", "hidden"}
    }


async def wait_for_step(
    page: Page,
    destination: str,
    action: str,
    allowed_frames: frozenset[str],
    before: set[tuple[str, str, str]],
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        confirmed = await observe(page, destination, action)
        if confirmed:
            digest, rule = next(iter(confirmed.items()))
            return {
                "kind": "confirmation",
                "evidence": {"confirmation_sha256": digest, "confirmation_rule": rule},
            }
        inspection = await inspect_page(
            page, destination=destination, allowed_frames=allowed_frames
        )
        after = signature(inspection)
        if before - after and after - before:
            return {"kind": "step", "inspection": inspection}
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("workflow_transition_unknown")
        await asyncio.sleep(0.2)
