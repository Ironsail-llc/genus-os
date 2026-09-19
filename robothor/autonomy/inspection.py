"""Broker-owned inspection: selectors, field metadata and narrow billing terms."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from robothor.autonomy.broker import price_minor, url_origin
from robothor.autonomy.terms import interval_months, renewal_date
from robothor.entity.audit import redact_for_audit
from robothor.secrets.redaction import redact

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from robothor.autonomy.privacy import ProtectedValues

_SELECTOR = r"""function selector(el) {
    const parts = [];
    while (el && el.nodeType === 1) {
        if (el.id) {
            const id = '#' + CSS.escape(el.id);
            if (el.ownerDocument.querySelectorAll(id).length === 1) {
                parts.unshift(id); break;
            }
        }
        const siblings = el.parentElement ? Array.from(el.parentElement.children).filter(s => s.tagName === el.tagName) : [el];
        parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(el)+1) + ')');
        el = el.parentElement;
    }
    return parts.join(' > ');
}"""

_INSPECT = (
    "() => {"
    + _SELECTOR
    + r"""
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const bounded = text => text.length <= 8192 ? text : '';
    const fields = Array.from(document.querySelectorAll('input,select,textarea,button')).filter(visible).slice(0,80).map(e => ({
        selector: selector(e), tag: e.tagName.toLowerCase(), type: e.type || '',
        id: e.id || '', name: e.name || '', required: !!e.required,
        label: bounded(e.labels?.[0]?.textContent || e.getAttribute('aria-label') ||
            (e.tagName === 'BUTTON' ? e.textContent : '') || ''),
        autocomplete: e.autocomplete || '', min_length: e.minLength >= 0 ? e.minLength : null,
        max_length: e.maxLength >= 0 ? e.maxLength : null,
        options: e.tagName === 'SELECT' ? Array.from(e.options).slice(0,80).map(o => bounded(o.label)) : []
    }));
    const names = ['total','subtotal','tax','shipping','annual','monthly','next charge',
        'next payment','renewal','billing','end date','expires','joining','membership','trial','recurring'];
    const candidates = Array.from(document.querySelectorAll('body *')).slice(0,4000)
        .filter(e => !e.children.length && visible(e) && !e.isContentEditable &&
            !e.closest('input,textarea,select,option,button,script,style,noscript,template,[contenteditable]'))
        .map(e => {
            const parent = e.parentElement;
            const context = ((parent && !parent.matches('body,form,section') ? parent.innerText : '') || '').slice(0,1000).toLowerCase();
            return {selector:selector(e),text:(e.innerText || '').trim().slice(0,101),
                labels:names.filter(name => context.includes(name))};
        }).filter(e => e.text && e.text.length <= 100).slice(0,400);
    return {fields, candidates};
}"""
)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(str(redact_for_audit(value)))
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def _term(candidate: dict[str, Any]) -> dict[str, Any] | None:
    text = candidate["text"]
    if _scrub(text) != text:
        return None
    if interval_months(text) is not None:
        return {**candidate, "kind": "interval", "interval_months": interval_months(text)}
    parsed_date = renewal_date(text)
    if parsed_date is not None and set(candidate["labels"]) & {
        "next charge",
        "next payment",
        "renewal",
        "billing",
        "end date",
        "expires",
        "trial",
    }:
        return {**candidate, "kind": "date", "date": parsed_date.isoformat()}
    currencies = []
    amount = None
    for currency in ("USD", "EUR", "GBP"):
        try:
            parsed = price_minor(text, currency)
            if parsed <= 10**12:
                currencies.append(currency)
                amount = parsed
        except ValueError:
            continue
    if currencies:
        return {**candidate, "kind": "amount", "amount_minor": amount, "currencies": currencies}
    return None


async def inspect_page(
    page: Page,
    *,
    destination: str,
    allowed_frames: frozenset[str],
    protected_values: ProtectedValues | None = None,
) -> dict[str, Any]:
    selector_script = (
        _SELECTOR.replace("if (el.id) {", "if (false && el.id) {")
        if protected_values
        else _SELECTOR
    )
    inspect_script = _INSPECT.replace(_SELECTOR, selector_script)
    fields: list[dict[str, Any]] = []
    terms: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    roots: list[tuple[Page | Frame, dict[str, Any]]] = [(page, {})]
    for frame in page.frames[:21]:
        if frame == page.main_frame:
            continue
        try:
            frame_origin = url_origin(frame.url)
        except ValueError:
            frames.append({"state": "unsupported_origin"})
            continue
        if frame.parent_frame != page.main_frame:
            frames.append({"origin": frame_origin, "state": "nested_frame_not_supported"})
            continue
        element = await frame.frame_element()
        frame_selector = await element.evaluate(
            "el => {" + selector_script + "return selector(el);}"
        )
        permitted = frame_origin == destination or frame_origin in allowed_frames
        frames.append(
            {
                "origin": frame_origin,
                "selector": frame_selector,
                "state": "ready" if permitted else "authority_required",
            }
        )
        if permitted:
            roots.append((frame, {"frame_selector": frame_selector, "frame_origin": frame_origin}))
    for root, binding in roots:
        data = await root.evaluate(inspect_script)
        # Recheck after evaluation: a frame navigation must not turn one
        # approved origin into a channel for an unapproved document.
        actual = root.url
        if url_origin(actual) != binding.get("frame_origin", destination):
            raise PermissionError("inspection_destination_changed")
        for field in data["fields"]:
            if len(field["selector"]) > 500:
                continue
            for key in ("id", "name", "label", "autocomplete"):
                value = field[key]
                field[key] = (protected_values.text(value) if protected_values else value)[:100]
            field["options"] = [
                (protected_values.text(value) if protected_values else value)[:100]
                for value in field["options"]
            ]
            fields.append({**field, **binding})
        for candidate in data["candidates"]:
            if len(candidate["selector"]) > 500:
                continue
            if protected_values and protected_values.text(candidate["text"]) != candidate["text"]:
                continue
            term = _term(candidate)
            if term:
                terms.append({**term, **binding})
    return cast(
        "dict[str, Any]",
        _scrub({"fields": fields[:160], "terms": terms[:80], "frames": frames[:20]}),
    )
