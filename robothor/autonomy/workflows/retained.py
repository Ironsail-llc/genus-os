"""Read-only observations from a frozen, uncertain submission page."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.autonomy.broker import url_origin
from robothor.autonomy.inspection import _SELECTOR, _scrub

if TYPE_CHECKING:
    from playwright.async_api import Page, Route

    from robothor.autonomy.models import Scope
    from robothor.autonomy.privacy import ProtectedValues
    from robothor.autonomy.workflows.manager import LiveWorkflow, WorkflowManager

_MESSAGES = (
    "() => {"
    + _SELECTOR.replace("if (el.id) {", "if (false && el.id) {")
    + r"""
    const elements=Array.from(document.querySelectorAll('[role="status"],[role="alert"],h1,h2,h3,p,div,span,li'));
    if (elements.length > 3000) return null;
    const values=elements.filter(e => !e.children.length && e.getClientRects().length &&
            getComputedStyle(e).visibility !== 'hidden' &&
            !e.closest('input,textarea,select,option,button,script,style,noscript,template,label,[contenteditable]'))
        .map(e => ({selector:selector(e),text:(e.innerText || '').trim()}))
        .filter(e => e.text.length >= 3 && e.text.length <= 300);
    return values.length > 80 ? null : values;
}"""
)


async def messages(page: Page, destination: str) -> list[dict[str, str]] | None:
    if url_origin(page.url) != destination:
        raise PermissionError("confirmation_origin_changed")
    values: list[dict[str, str]] | None = await page.evaluate(_MESSAGES)
    if url_origin(page.url) != destination:
        raise PermissionError("confirmation_origin_changed")
    if values is None:
        return None
    return [dict(item, digest=hashlib.sha256(item["text"].encode()).hexdigest()) for item in values]


def public_message(item: dict[str, str], protected: ProtectedValues) -> dict[str, str]:
    return {"selector": item["selector"], "text": _scrub(protected.text(item["text"]))}


async def freeze(live: LiveWorkflow) -> None:
    if live.frozen:
        return

    async def abort(route: Route) -> None:
        await route.abort()

    # This route takes precedence over merchant routing, including mocked HTTP.
    await live.page.context.route("**/*", abort)
    await live.page.context.set_offline(True)
    live.frozen = True


async def candidates(live: LiveWorkflow, destination: str) -> list[dict[str, str]]:
    if live.broker._used_transient_code or live.broker.reconciliation_baseline is None:
        return []
    live.broker.protected_values.session(dict(await live.page.context.storage_state()))
    observed = await messages(live.page, destination)
    return [
        item
        for item in observed or []
        if len(item["selector"]) <= 500
        and item["digest"] not in live.broker.reconciliation_baseline
    ]


async def inspect(live: LiveWorkflow, destination: str) -> dict[str, Any]:
    await freeze(live)
    return {
        "state": "reconciling",
        "reason": "protected_challenge_requires_external_reconciliation"
        if live.broker._used_transient_code
        else "read_only_confirmation_required",
        "confirmations": [
            public_message(item, live.broker.protected_values)
            for item in await candidates(live, destination)
        ],
    }


async def reconcile(
    manager: WorkflowManager,
    scope: Scope,
    agent_id: str,
    workflow_id: str,
    command_id: str,
    revision: int,
    selector: str,
    text: str,
) -> dict[str, Any]:
    row = await asyncio.to_thread(manager.repository.get, scope, agent_id, workflow_id)
    live = manager._live.get(workflow_id)
    lock = live.lock if live else asyncio.Lock()
    async with lock:
        request = {"kind": "reconcile", "revision": revision, "selector": selector, "text": text}
        prior = await asyncio.to_thread(
            manager.repository.begin,
            scope,
            agent_id,
            workflow_id,
            manager.instance_id,
            command_id,
            request,
        )
        if prior is not None:
            return prior
        try:
            operation = await asyncio.to_thread(manager.store.operation, scope, row["operation_id"])
            if not live or manager._expired(live) or operation["state"] != "reconciling":
                raise PermissionError("pending_live_confirmation_required")
            await freeze(live)
            observed = await candidates(live, row["origin"])
            matches = [
                item
                for item in observed
                if public_message(item, live.broker.protected_values)
                == {"selector": selector, "text": text}
            ]
            result: dict[str, Any] = {
                "workflow_id": workflow_id,
                "operation_id": row["operation_id"],
                "revision": revision,
                "state": "reconciling",
                "reason": "confirmation_not_found",
            }
            if len(matches) == 1:
                evidence = {
                    "origin": row["origin"],
                    "confirmation_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "verified_at": datetime.now(UTC).isoformat(),
                    "kind": "reconciled_confirmation",
                }
                await asyncio.to_thread(
                    manager.store.finish, scope, row["operation_id"], "completed", evidence
                )
                result = {
                    "workflow_id": workflow_id,
                    "operation_id": row["operation_id"],
                    "revision": revision + 1,
                    "state": "completed",
                    "evidence": evidence,
                }
            completed = result["state"] == "completed"
            await asyncio.to_thread(
                manager.repository.finish,
                scope,
                agent_id,
                workflow_id,
                command_id,
                result,
                advance=completed,
            )
            live.last_used = manager.clock()
            if completed:
                await manager._discard(workflow_id, live, "completed")
            return result
        except BaseException:
            if live and manager._live.get(workflow_id) is live:
                await manager._discard(workflow_id, live)
            raise
