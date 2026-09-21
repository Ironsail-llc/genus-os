"""Validate native constraints in a separate offline browser realm before filling.

Merchant code never receives candidate values during this check. Only fixed
constraint names and selectors leave the broker, never native error messages.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from robothor.autonomy.inspection import _SELECTOR, _scrub

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

    from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
    from robothor.autonomy.models import Scope, WebOperation

_DESCRIBE = r"""function describe(e) {
    if (!['INPUT','SELECT','TEXTAREA'].includes(e.tagName)) return null;
    const attrs = {};
    for (const name of ['type','required','disabled','readonly','multiple','min','max','step','pattern','minlength','maxlength','name'])
        if (e.hasAttribute(name)) attrs[name] = e.getAttribute(name);
    if (e.matches(':disabled')) attrs.disabled='';
    return {selector:selector(e),form:e.form ? selector(e.form) : '',tag:e.tagName.toLowerCase(),attrs,
        value:e.type === 'file' ? '' : e.value,checked:!!e.checked,hasFile:!!e.files?.length,
        options:e.tagName === 'SELECT' ? Array.from(e.options).map(o=>({value:o.value,label:o.label,disabled:o.disabled})) : []};
}"""
_ONE = "e => {" + _SELECTOR + _DESCRIBE + "return describe(e); }"
_FORM = (
    "e => {"
    + _SELECTOR
    + _DESCRIBE
    + "const f=e.form || e.closest('form'); return f ? Array.from(f.elements).slice(0,160).map(describe).filter(Boolean) : []; }"
)
_VALIDATE = r"""specs => {
    const forms=new Map();
    const fields=specs.map(s=>{
        const group=JSON.stringify([s.frame,s.form]);
        if (!forms.has(group)) { const f=document.createElement('form'); document.body.append(f); forms.set(group,f); }
        const form=forms.get(group);
        const e=document.createElement(s.tag);
        for (const [key,value] of Object.entries(s.attrs)) e.setAttribute(key,value);
        for (const option of s.options) {
            const o=document.createElement('option'); o.value=option.value;
            o.textContent=option.label; o.disabled=option.disabled; e.append(o);
        }
        if (e.type === 'file') {
            if (s.hasFile) { const dt=new DataTransfer(); dt.items.add(new File([''], 'document')); e.files=dt.files; }
        } else e.value=s.value;
        if (e.type === 'checkbox' || e.type === 'radio') e.checked=s.checked;
        form.append(e); return e;
    });
    const keys=['valueMissing','typeMismatch','patternMismatch','rangeUnderflow','rangeOverflow','stepMismatch','badInput'];
    const results=fields.map((e,i)=>{
        if (specs[i].method && e.disabled) return ['fieldDisabled'];
        if (specs[i].method === 'fill' && e.readOnly) return ['fieldReadOnly'];
        if (!e.willValidate) return [];
        const problems=keys.filter(k=>e.validity[k]);
        // Browser length flags ignore programmatically supplied values.
        if (['text','email','search','tel','url','password'].includes(e.type) || e.tagName === 'TEXTAREA') {
            if (e.minLength >= 0 && e.value.length && e.value.length < e.minLength) problems.push('tooShort');
            if (e.maxLength >= 0 && e.value.length > e.maxLength) problems.push('tooLong');
        }
        if (e.tagName === 'SELECT' && !Array.from(e.options).some(o=>o.value===specs[i].value && !o.disabled)) problems.push('optionMissing');
        return problems;
    });
    for (const form of forms.values()) form.remove(); return results;
}"""


async def validate_plan(
    broker: BrowserBroker,
    scope: Scope,
    page: Page,
    proposal: WebOperation,
    plan: ExecutionPlan,
    allowed_frames: frozenset[str],
) -> list[dict[str, Any]]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    one = _ONE.replace("if (el.id) {", "if (false && el.id) {") if broker.protected_values else _ONE
    form = (
        _FORM.replace("if (el.id) {", "if (false && el.id) {") if broker.protected_values else _FORM
    )

    async def collect(locator: Locator, frame: dict[str, str]) -> tuple[str, str] | None:
        for item in await locator.evaluate(form):
            key = (frame.get("frame_selector", ""), item["selector"])
            entries.setdefault(key, {**item, "frame": frame})
        item = await locator.evaluate(one)
        if item is None:
            return None
        key = (frame.get("frame_selector", ""), item["selector"])
        entries.setdefault(key, {**item, "frame": frame})
        return key

    await collect(await broker._unique(page.locator(plan.submit_selector)), {})
    for binding in plan.fields:
        if binding.kind == "payment_card" and proposal.action not in {"purchase", "subscription"}:
            raise PermissionError("payment_authority_required")
        root, destination = await broker._binding_root(
            page, binding, proposal.origin, allowed_frames
        )
        locator = await broker._unique(root.locator(binding.selector))
        frame = (
            {"frame_selector": binding.frame_selector, "frame_origin": destination}
            if binding.frame_selector
            else {}
        )
        key = await collect(locator, frame)
        if key is None:
            raise ValueError("unsupported_field_element")
        entries[key]["method"] = binding.method
        value = await asyncio.to_thread(
            broker.store.consume_resource,
            scope,
            str(binding.resource_id),
            destination,
            kind=binding.kind,
        )
        if binding.kind == "document":
            if binding.method != "upload":
                raise ValueError("document_requires_upload")
            entries[key]["hasFile"] = bool(value["base64"])
        else:
            entries[key]["value"] = broker._binding_text(binding, value)
    for selector in plan.check_selectors:
        key = await collect(await broker._unique(page.locator(selector)), {})
        if key is not None:
            entries[key]["checked"] = True
            entries[key]["method"] = "check"
    # The transient code is checked only after secure input is supplied. Do not
    # treat its intentionally empty field as an incomplete personal profile.
    if plan.challenge:
        challenge_root = page
        frame_selector = plan.challenge.frame_selector
        if frame_selector:
            from robothor.autonomy.broker import url_origin

            challenge_destination = plan.challenge.frame_origin
            if not challenge_destination or (
                challenge_destination != proposal.origin
                and challenge_destination not in allowed_frames
            ):
                raise PermissionError("frame_not_authorized")
            element = await page.locator(frame_selector).element_handle()
            challenge_frame = await element.content_frame() if element else None
            if not challenge_frame or url_origin(challenge_frame.url) != challenge_destination:
                raise PermissionError("frame_origin_mismatch")
            item = await challenge_frame.locator(plan.challenge.selector).evaluate(one)
        else:
            item = await challenge_root.locator(plan.challenge.selector).evaluate(one)
        if item:
            entries.pop((frame_selector or "", item["selector"]), None)
    browser = page.context.browser
    if browser is None:
        raise RuntimeError("validation_browser_unavailable")
    isolated = await browser.new_context(offline=True, service_workers="block")
    try:
        validation_page = await isolated.new_page()
        specs = list(entries.values())
        issues = await validation_page.evaluate(_VALIDATE, specs)
        return [
            _scrub({"selector": item["selector"], **item["frame"], "issues": problems})
            for item, problems in zip(specs, issues, strict=True)
            if problems
        ]
    finally:
        await isolated.close()
