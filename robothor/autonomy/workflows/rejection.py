"""Require a matching rejected POST and fresh form errors before retrying."""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.autonomy.broker import url_origin

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response

    from robothor.autonomy.broker import ExecutionPlan


# Error messages can contain passwords, codes or personal values. Only this
# bounded category leaves the protected realm; never persist their text/hash.
_ERROR = """(el, form) => {
  if (!form.contains(el) || el.getAttribute('aria-invalid') !== 'true' ||
      !el.checkVisibility()) return null;
  const ids = [el.getAttribute('aria-errormessage'), el.getAttribute('aria-describedby')]
    .filter(Boolean).join(' ').split(/\\s+/);
  const messages = ids.map(id => el.ownerDocument.getElementById(id)).filter(node =>
    node && form.contains(node) && node.checkVisibility() && !node.isContentEditable &&
    !['INPUT','TEXTAREA','SELECT'].includes(node.tagName));
  const text = messages.map(node => node.innerText || '').join(' ').trim().toLowerCase();
  if (!text) return null;
  if (/cannot be used|already (used|registered|taken)|unavailable/.test(text)) return 'value_unavailable';
  if (/required|must (enter|provide)/.test(text)) return 'required';
  if (/invalid|not valid|incorrect format/.test(text)) return 'invalid_format';
  return 'server_field_rejected';
}"""


class RejectionWatch:
    def __init__(self, page: Page, submit_selector: str, action: str, selectors: list[str]) -> None:
        self.page = page
        self.submit_selector = submit_selector
        self.action = action
        self.selectors = selectors
        self.before: set[str] = set()
        self.requests: set[Request] = set()
        self.responses: dict[Request, int] = {}

    @classmethod
    async def create(cls, page: Page, plan: ExecutionPlan) -> RejectionWatch | None:
        handle = await page.locator(plan.submit_selector).evaluate_handle(
            "el => el.form || el.closest('form')"
        )
        form = handle.as_element()
        if form is None:
            await handle.dispose()
            return None
        details = await form.evaluate("el => ({action:el.action,method:el.method})")
        if details["method"].lower() != "post" or url_origin(details["action"]) != url_origin(
            page.url
        ):
            await form.dispose()
            return None
        await form.dispose()
        watch = cls(
            page,
            plan.submit_selector,
            details["action"],
            [field.selector for field in plan.fields if not field.frame_selector],
        )
        watch.before = {field["selector"] for field in await watch.errors()}
        return watch

    async def errors(self) -> list[dict[str, str]]:
        errors = []
        button = self.page.locator(self.submit_selector)
        if await button.count() != 1:
            return []
        handle = await button.evaluate_handle("el => el.form || el.closest('form')")
        try:
            form = handle.as_element()
            if form is None:
                return []
            details = await form.evaluate("el => ({action:el.action,method:el.method})")
            if details["action"] != self.action or details["method"].lower() != "post":
                return []
            for selector in self.selectors:
                field = self.page.locator(selector)
                if await field.count() != 1:
                    continue
                reason = await field.evaluate(_ERROR, form)
                if reason:
                    errors.append({"selector": selector, "reason": reason})
        finally:
            await handle.dispose()
        return errors

    def _request(self, request: Request) -> None:
        if (
            request.method == "POST"
            and request.url == self.action
            and request.frame == self.page.main_frame
        ):
            self.requests.add(request)

    def _response(self, response: Response) -> None:
        if response.request in self.requests:
            self.responses[response.request] = response.status

    def start(self) -> None:
        self.page.on("request", self._request)
        self.page.on("response", self._response)

    async def rejected_fields(self) -> list[dict[str, str]]:
        if len(self.requests) != 1 or list(self.responses.values()) != [422]:
            return []
        return [field for field in await self.errors() if field["selector"] not in self.before]

    async def close(self) -> None:
        self.page.remove_listener("request", self._request)
        self.page.remove_listener("response", self._response)
        self.requests.clear()
        self.responses.clear()
