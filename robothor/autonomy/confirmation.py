"""Recognize affirmative merchant messages without exporting their text."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import TYPE_CHECKING

from robothor.autonomy.broker import url_origin

if TYPE_CHECKING:
    from playwright.async_api import Page

_RULES = {
    "account": (
        "account_created",
        r"(?:your )?account (?:has been |was |is )?(?:successfully )?(?:created|registered)(?: successfully)?",
    ),
    "application": (
        "application_received",
        r"(?:your )?application (?:has been |was |is )?(?:successfully )?(?:submitted|received)(?: successfully)?",
    ),
    "purchase": (
        "order_confirmed",
        r"(?:your )?order (?:has been |was |is )?(?:successfully )?(?:placed|confirmed|received|completed)(?: successfully)?",
    ),
    "subscription": (
        "membership_active",
        r"(?:your )?(?:membership|subscription) (?:(?:has been |was )?(?:successfully )?(?:activated|renewed)|is (?:now )?active|active|confirmed)",
    ),
    "login": (
        "login_confirmed",
        r"(?:you (?:have |are )?(?:successfully )?(?:signed|logged) in|(?:sign|log)[ -]?in successful)",
    ),
}
_TEXTS = r"""() => Array.from(document.querySelectorAll('[role="status"],[role="alert"],h1,h2,h3,p,div,span,li'))
    .slice(0,3000).filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' &&
        !e.closest('input,textarea,select,option,button,script,style,noscript,template,label,[contenteditable]') &&
        !e.querySelector('input,textarea,select,option,button,script,style,[contenteditable]'))
    .map(e => (e.innerText || '').trim()).filter(text => text.length > 0 && text.length <= 200)"""


def classify(text: str, action: str) -> str | None:
    rule = _RULES.get(action)
    if rule is None:
        return None
    normalized = " ".join(text.split())
    return rule[0] if re.fullmatch(rule[1] + r"[.!]?", normalized, re.IGNORECASE) else None


async def observe(page: Page, destination: str, action: str) -> dict[str, str]:
    if url_origin(page.url) != destination:
        raise PermissionError("confirmation_origin_changed")
    texts = await page.evaluate(_TEXTS)
    if url_origin(page.url) != destination:
        raise PermissionError("confirmation_origin_changed")
    result = {}
    for text in texts:
        rule = classify(text, action)
        if rule:
            result[hashlib.sha256(text.encode()).hexdigest()] = rule
    return result


async def wait_for_confirmation(
    page: Page, destination: str, action: str, *, timeout_seconds: float = 30
) -> dict[str, str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        matches = await observe(page, destination, action)
        if matches:
            digest, rule = next(iter(matches.items()))
            return {"confirmation_sha256": digest, "confirmation_rule": rule}
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("confirmation_not_found")
        await asyncio.sleep(0.2)
