"""Exclusive, reference-only browser execution.

The broker owns its browser and never registers it in the general browser
tool's session map. Execute in the worker process; the engine gets only state
and hashed confirmation evidence. No screenshots, traces or page evaluation
are exposed while credential-bearing operations execute.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import re
import struct
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID  # noqa: TC003 - Pydantic resolves this annotation at runtime

from pydantic import Field, SecretStr, field_validator, model_serializer, model_validator

from robothor.autonomy.material_documents import MaterialTermsUnavailableError, MaterialTermTarget
from robothor.autonomy.models import ResourceInput, Scope, StrictModel, WebOperation, origin
from robothor.autonomy.terms import interval_months, renewal_date

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import Frame, Locator, Page, Route

    from robothor.autonomy.store import AutonomyStore


class FieldBinding(StrictModel):
    selector: str = Field(min_length=1, max_length=500)
    resource_id: UUID
    kind: Literal["profile", "credential", "document", "totp", "payment_card"]
    field: str = Field(min_length=1, max_length=80)
    method: Literal["fill", "select", "upload"] = "fill"
    frame_selector: str | None = Field(default=None, max_length=500)
    frame_origin: str | None = None

    @field_validator("frame_origin")
    @classmethod
    def valid_frame(cls, value: str | None) -> str | None:
        return origin(value) if value else None


class Challenge(StrictModel):
    selector: str = Field(min_length=1, max_length=500)
    kind: Literal["card_code", "one_time_code"]
    frame_selector: str | None = None
    frame_origin: str | None = None

    _frame_origin = field_validator("frame_origin")(lambda value: origin(value) if value else None)


class ExecutionPlan(StrictModel):
    url: str = Field(max_length=2000)
    fields: list[FieldBinding] = Field(default_factory=list, max_length=80)
    check_selectors: list[str] = Field(default_factory=list, max_length=20)
    material_terms: list[MaterialTermTarget] = Field(default_factory=list, max_length=5)
    submit_selector: str = Field(min_length=1, max_length=500)
    success_selector: str | None = Field(default=None, min_length=1, max_length=500)
    success_text: str | None = Field(default=None, min_length=3, max_length=300)
    terms_frame_selector: str | None = Field(default=None, max_length=500)
    terms_frame_origin: str | None = None
    amount_selector: str | None = Field(default=None, max_length=500)
    recurring_selector: str | None = Field(default=None, max_length=500)
    annual_selector: str | None = Field(default=None, max_length=500)
    recurrence_interval_selector: str | None = Field(default=None, max_length=500)
    next_charge_selector: str | None = Field(default=None, max_length=500)
    recurrence_end_selector: str | None = Field(default=None, max_length=500)
    session_resource_id: UUID | None = None
    verification_link_id: UUID | None = None
    challenge: Challenge | None = None

    _terms_origin = field_validator("terms_frame_origin")(
        lambda value: origin(value) if value else None
    )

    @model_serializer(mode="wrap")
    def compatible_plan(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.material_terms:
            data.pop("material_terms", None)
        return data

    @model_validator(mode="after")
    def confirmation_pair(self) -> ExecutionPlan:
        if bool(self.success_selector) != bool(self.success_text):
            raise ValueError("confirmation_selector_and_text_required_together")
        return self

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url_origin(value)
        return value


def url_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError("credentialed_url")
    return origin(f"{parsed.scheme}://{parsed.netloc}")


#: A declared confirmation criterion is at most 300 characters, so the element
#: it is read from may not carry more text than that either.
MAX_CONFIRMATION_TEXT = 300
#: A flat ceiling alone was satisfied by padding: a 277-character node
#: reading "Order confirmed. " + 260 filler characters completed an
#: operation. Scaling the ceiling by the declared phrase instead was worse --
#: it refused an ordinary panel at 121 characters and its only workaround was
#: for the agent to guess more future wording, the behaviour #621 removed.
#: What separates the two is shape, not size: merchant prose is words. Order
#: numbers, tracking codes and dates are long tokens, so this is generous.
MAX_CONFIRMATION_TOKEN = 24
#: One submission cannot have visited an unbounded number of pages.
MAX_LANDED_PAGES = 10


def same_page(left: str, right: str) -> bool:
    """Full-URL identity: scheme, host, port, path and query. Not just origin.

    Pinning the origin alone let a status check read any other page the site
    happened to serve, including a help article quoting "Your order has been
    confirmed". The fragment is ignored: it never reaches the server.

    Total by construction: anything this cannot parse, or that carries
    credentials, is simply not the same page. It is called from guard
    positions outside a try, where a raised ValueError would surface as
    ``broker_request_failed`` instead of a refusal.
    """
    try:
        first, second = urlsplit(left), urlsplit(right)
        if first.username or first.password or second.username or second.password:
            return False
        return (
            url_origin(left) == url_origin(right)
            and (first.path or "/") == (second.path or "/")
            and first.query == second.query
        )
    except ValueError:
        return False


def totp(secret: str, timestamp: float | None = None) -> str:
    key = base64.b32decode(secret.upper(), casefold=True)
    digest = hmac.new(
        key,
        struct.pack(">Q", int(timestamp if timestamp is not None else time.time()) // 30),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1000000:06d}"


def price_minor(text: str, currency: str) -> int:
    # Deliberately strict: ambiguous prices must not silently become a smaller
    # amount. The selector must identify one visible total, not a whole cart.
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency)
    if not symbol:
        raise ValueError("unsupported_currency_format")
    pattern = (
        rf"\s*(?:{currency}\s*)?{re.escape(symbol)}?\s*(\d[\d,]*\.\d{{2}})\s*(?:{currency})?\s*"
    )
    match = re.fullmatch(pattern, text)
    if not match:
        raise ValueError("ambiguous_price")
    raw = match[1]
    if "," in raw and not re.fullmatch(r"\d{1,3}(,\d{3})+\.\d{2}", raw):
        raise ValueError("ambiguous_price")
    return int(Decimal(raw.replace(",", "")) * 100)


class BrowserBroker:
    def __init__(
        self,
        store: AutonomyStore,
        *,
        public_document_router: Callable[[Route], Awaitable[None]] | None = None,
    ) -> None:
        self.public_document_router = public_document_router
        from robothor.autonomy.privacy import ProtectedValues

        self.store = store
        self.protected_values = ProtectedValues()
        self.reconciliation_baseline: set[str] | None = None
        self._used_transient_code = False
        self._guarded_frames: dict[Frame, str] = {}

    def _confirmation_digest(self, text: str, plan: ExecutionPlan) -> str:
        # A merchant can echo a code in an arbitrary encoding. After code entry,
        # hash only the already-declared, matched phrase, not arbitrary DOM text.
        # The plan predates the transient code and is already in the journal.
        public_text = plan.success_text or ""
        source = (
            public_text
            if self._used_transient_code or plan.challenge
            else self.protected_values.text(text)
        )
        return hashlib.sha256(source.encode()).hexdigest()

    async def inspect(
        self, page: Page, destination: str, allowed_frames: frozenset[str]
    ) -> dict[str, Any]:
        from robothor.autonomy.inspection import inspect_page

        return await inspect_page(
            page,
            destination=destination,
            allowed_frames=allowed_frames,
            protected_values=self.protected_values,
        )

    async def _guard_origin(self, page: Page, frame: Frame, destination: str) -> None:
        if frame in self._guarded_frames and self._guarded_frames[frame] != destination:
            raise PermissionError("frame_origin_mismatch")
        if frame not in self._guarded_frames:

            async def protect(route: Route) -> None:
                if route.request.is_navigation_request() and route.request.frame == frame:
                    try:
                        permitted = url_origin(route.request.url) == destination
                    except ValueError:
                        permitted = False
                    if not permitted:
                        await route.abort()
                        return
                await route.fallback()

            await page.route("**/*", protect)
            self._guarded_frames[frame] = destination
        current_url = page.url if frame == page.main_frame else frame.url
        if url_origin(current_url) != destination:
            raise PermissionError("destination_changed")

    async def verify_link_on_page(
        self, scope: Scope, operation_id: str, agent_id: str, plan: ExecutionPlan, page: Page
    ) -> dict[str, Any]:
        row = await asyncio.to_thread(self.store.operation, scope, operation_id)
        if row["agent_id"] != agent_id or row["proposal"]["action"] != "login" or plan.fields:
            raise PermissionError("verification_authority_required")
        if row["state"] != "reserved":
            return {"operation_id": operation_id, "state": row["state"]}
        if not plan.success_selector or not plan.success_text:
            raise ValueError("specific_confirmation_required")
        destination = row["proposal"]["origin"]
        value = await asyncio.to_thread(
            self.store.consume_resource,
            scope,
            str(plan.verification_link_id),
            destination,
            kind="credential",
        )
        url = value["password"]
        self.protected_values.add(url)
        if url_origin(url) != destination:
            raise PermissionError("verification_origin_mismatch")
        await asyncio.to_thread(self.store.begin_submit, scope, operation_id, agent_id)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if url_origin(page.url) != destination:
                raise PermissionError("verification_origin_mismatch")
            confirmation = page.locator(plan.success_selector)
            await confirmation.wait_for(state="visible", timeout=15000)
            text = await (await self._unique(confirmation)).inner_text()
            if plan.success_text not in text:
                raise ValueError("confirmation_missing")
            evidence = {
                "origin": destination,
                "confirmation_sha256": self._confirmation_digest(text, plan),
                "verified_at": datetime.now(UTC).isoformat(),
                "kind": "verification_confirmation",
            }
            await asyncio.to_thread(self.store.finish, scope, operation_id, "completed", evidence)
            session_ref = None
            with contextlib.suppress(Exception):
                saved = await asyncio.to_thread(
                    self.store.put_resource,
                    scope,
                    ResourceInput(
                        kind="browser_session",
                        label="Verified website session",
                        origin=destination,
                        payload=SecretStr(json.dumps(await page.context.storage_state())),
                    ),
                    source="broker_session",
                )
                session_ref = saved["id"]
            return {
                "operation_id": operation_id,
                "state": "completed",
                "session_resource_id": session_ref,
                "evidence": evidence,
            }
        except Exception:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.store.finish, scope, operation_id, "reconciling")
            return {"operation_id": operation_id, "state": "reconciling"}
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.revoke_resource, scope, str(plan.verification_link_id)
                )

    async def reconcile_on_page(
        self, scope: Scope, operation_id: str, agent_id: str, plan: ExecutionPlan, page: Page
    ) -> dict[str, Any]:
        row = await asyncio.to_thread(self.store.operation, scope, operation_id)
        if row["agent_id"] != agent_id:
            raise PermissionError("agent_not_allowed")
        if row["state"] == "completed":
            return row
        if row["state"] not in {"submitting", "reconciling", "awaiting_input"}:
            raise PermissionError("operation_not_pending")
        if plan.fields or plan.check_selectors:
            raise PermissionError("reconciliation_is_read_only")
        destination = row["proposal"]["origin"]
        if url_origin(plan.url) != destination:
            raise PermissionError("destination_mismatch")
        await asyncio.to_thread(self.store.check_reconcile_authority, scope, operation_id, agent_id)
        if not plan.success_selector and row["proposal"]["action"] in {"purchase", "subscription"}:
            # The automatic path is the SUBMISSION classifier: it accepts an
            # affirmative sentence anywhere on the origin. That is not a basis
            # for declaring money settled.
            return {
                "operation_id": operation_id,
                "state": row["state"],
                "reason": "specific_confirmation_required_for_payment",
            }
        from robothor.autonomy.handoffs import observed_submission_pages

        if not any(same_page(plan.url, landed) for landed in observed_submission_pages(row)):
            # Origin was the only pin, so any same-origin page the caller named
            # would do -- a help article matched a checkout.
            return {
                "operation_id": operation_id,
                "state": row["state"],
                "reason": "confirmation_page_not_registered",
            }
        if row["state"] != "reconciling":
            await asyncio.to_thread(self.store.finish, scope, operation_id, "reconciling")
        try:
            from robothor.autonomy.readonly_browser import restrict_reconciliation

            await restrict_reconciliation(page)
            await page.goto(plan.url, wait_until="domcontentloaded", timeout=30000)
            if not same_page(page.url, plan.url):
                # Path and query, not just origin: the page that answers must
                # be the page that was pinned in the durable handoff.
                raise PermissionError("confirmation_page_changed")
            if plan.success_selector:
                locator = page.locator(plan.success_selector)
                await locator.wait_for(state="visible", timeout=15000)
                text = await self._specific_text(locator)
                if not plan.success_text or plan.success_text not in text:
                    raise ValueError("confirmation_missing")
                proof = {"confirmation_sha256": self._confirmation_digest(text, plan)}
            else:
                from robothor.autonomy.confirmation import wait_for_confirmation

                proof = await wait_for_confirmation(
                    page, destination, row["proposal"]["action"], timeout_seconds=15
                )
            if not same_page(page.url, plan.url):
                raise PermissionError("confirmation_page_changed")
            evidence = {
                "origin": destination,
                **proof,
                "verified_at": datetime.now(UTC).isoformat(),
                "kind": "reconciled_confirmation",
            }
            await asyncio.to_thread(self.store.finish, scope, operation_id, "completed", evidence)
            from robothor.autonomy.receipt_capture import capture_receipt

            receipt = await capture_receipt(self, scope, operation_id, agent_id, page, plan=plan)
            return {
                "operation_id": operation_id,
                "state": "completed",
                "evidence": evidence,
                **({"receipt": receipt} if receipt is not None else {}),
            }
        except Exception:
            return {
                "operation_id": operation_id,
                "state": "reconciling",
                "reason": "confirmation_not_found",
            }

    async def _record_landings(
        self, scope: Scope, operation_id: str, landed: list[str], page: Page
    ) -> None:
        """Never let bookkeeping change an outcome the merchant already gave.

        A workflow step can settle without a navigation event, so the current
        address is recorded too.
        """
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                self.store.record_landed_pages, scope, operation_id, [*landed, page.url]
            )

    @staticmethod
    async def _unique(locator: Locator) -> Locator:
        if await locator.count() != 1 or not await locator.is_visible():
            raise ValueError("selector_not_unique_and_visible")
        return locator

    @classmethod
    async def _specific_text(cls, locator: Locator) -> str:
        """Bound the element a confirmation may be read from, at check time.

        The creation-time denylist held four literals, so ``html body``,
        ``main``, ``p``, ``div``, ``body *`` and ``body > *`` all walked past
        it and ``html body`` completed an operation on a page that read "Your
        order is still pending". What makes a selector specific is not its
        spelling: it is that it matches exactly one element carrying a short,
        word-shaped message rather than a page.
        """
        text = await (await cls._unique(locator)).inner_text()
        tokens = text.split()
        normalized = " ".join(tokens)
        if len(normalized) > MAX_CONFIRMATION_TEXT or (
            tokens and len(normalized) / len(tokens) > MAX_CONFIRMATION_TOKEN
        ):
            raise ValueError("confirmation_element_not_specific")
        return text

    async def _prices(
        self,
        page: Page,
        proposal: WebOperation,
        plan: ExecutionPlan,
        *,
        allowed_frames: frozenset[str] = frozenset(),
    ) -> None:
        root: Page | Frame = page
        if plan.terms_frame_selector:
            if not plan.terms_frame_origin or (
                plan.terms_frame_origin != proposal.origin
                and plan.terms_frame_origin not in allowed_frames
            ):
                raise PermissionError("frame_not_authorized")
            element = await page.locator(plan.terms_frame_selector).element_handle()
            frame = await element.content_frame() if element else None
            if not frame or url_origin(frame.url) != plan.terms_frame_origin:
                raise PermissionError("frame_origin_mismatch")
            await self._guard_origin(page, frame, plan.terms_frame_origin)
            root = frame
        await self._recurrence(root, proposal, plan)
        for amount, selector, required in [
            (
                proposal.amount_minor,
                plan.amount_selector,
                proposal.action in {"purchase", "subscription"},
            ),
            (proposal.recurring_minor, plan.recurring_selector, False),
            (proposal.annual_commitment_minor, plan.annual_selector, False),
        ]:
            if amount or selector or required:
                if not selector:
                    raise ValueError("price_evidence_required")
                locator = await self._unique(root.locator(selector))
                if price_minor(await locator.inner_text(), proposal.currency) != amount:
                    raise ValueError("price_changed")

    async def _recurrence(
        self, page: Page | Frame, proposal: WebOperation, plan: ExecutionPlan
    ) -> None:
        terms = proposal.recurrence
        if not proposal.recurring_minor:
            return
        if not terms or not plan.recurrence_interval_selector or not plan.next_charge_selector:
            raise ValueError("renewal_evidence_required")
        interval = await self._unique(page.locator(plan.recurrence_interval_selector))
        if interval_months(await interval.inner_text()) != terms.interval_months:
            raise ValueError("recurrence_changed")
        for expected, selector in (
            (terms.next_charge_on, plan.next_charge_selector),
            (terms.ends_on, plan.recurrence_end_selector),
        ):
            if expected is None:
                continue
            if not selector:
                raise ValueError("renewal_evidence_required")
            value = (await (await self._unique(page.locator(selector))).inner_text()).strip()
            if renewal_date(value) != expected:
                raise ValueError("renewal_date_changed")

    async def _binding_root(
        self,
        page: Page,
        binding: FieldBinding,
        destination: str,
        allowed_frames: frozenset[str],
    ) -> tuple[Page | Frame, str]:
        if url_origin(page.url) != destination:
            raise PermissionError("destination_changed")
        root: Page | Frame = page
        resource_destination = destination
        guarded_frame = page.main_frame
        if binding.frame_selector:
            if not binding.frame_origin or (
                binding.frame_origin != destination and binding.frame_origin not in allowed_frames
            ):
                raise PermissionError("frame_not_authorized")
            element = await page.locator(binding.frame_selector).element_handle()
            frame = await element.content_frame() if element else None
            if not frame or url_origin(frame.url) != binding.frame_origin:
                raise PermissionError("frame_origin_mismatch")
            root = frame
            guarded_frame = frame
            resource_destination = binding.frame_origin
        await self._guard_origin(page, guarded_frame, resource_destination)
        return root, resource_destination

    @staticmethod
    def _binding_text(binding: FieldBinding, value: dict[str, Any]) -> str:
        if binding.kind == "totp":
            return totp(value["secret"])
        if binding.kind == "profile" and binding.field.startswith("answers."):
            return str(value["answers"][binding.field.removeprefix("answers.")])
        if binding.field not in value:
            raise ValueError("resource_field_missing")
        return str(value[binding.field])

    async def _field(
        self,
        scope: Scope,
        page: Page,
        binding: FieldBinding,
        destination: str,
        allowed_frames: frozenset[str],
    ) -> None:
        root, resource_destination = await self._binding_root(
            page, binding, destination, allowed_frames
        )
        value = await asyncio.to_thread(
            self.store.consume_resource,
            scope,
            str(binding.resource_id),
            resource_destination,
            kind=binding.kind,
        )
        locator = await self._unique(root.locator(binding.selector))
        if binding.kind == "document":
            if binding.method != "upload":
                raise ValueError("document_requires_upload")
            self.protected_values.add(value["name"])
            await locator.set_input_files(
                {
                    "name": value["name"],
                    "mimeType": value["mime_type"],
                    "buffer": base64.b64decode(value["base64"]),
                }
            )
            return
        text = self._binding_text(binding, value)
        self.protected_values.add(text)
        if binding.kind == "totp":
            self._used_transient_code = True
        if binding.method == "select":
            await locator.select_option(text, timeout=15000)
        elif binding.method == "fill":
            await locator.fill(text, timeout=15000)
        else:
            raise ValueError("unsupported_field_method")

    async def execute_on_page(
        self,
        scope: Scope,
        operation_id: str,
        agent_id: str,
        plan: ExecutionPlan,
        page: Page,
        *,
        allowed_frames: frozenset[str] = frozenset(),
        verification_code: str | None = None,
        workflow_id: str | None = None,
        navigate: bool = True,
        advance: bool = False,
    ) -> dict[str, Any]:
        if plan.verification_link_id:
            if workflow_id:
                raise PermissionError("workflow_verification_link_not_supported")
            return await self.verify_link_on_page(scope, operation_id, agent_id, plan, page)
        row = await asyncio.to_thread(self.store.operation, scope, operation_id)
        if row.get("workflow_id") != workflow_id:
            raise PermissionError("workflow_required")
        if row["agent_id"] != agent_id:
            raise PermissionError("agent_not_allowed")
        if row["state"] != "reserved":
            return {
                "operation_id": operation_id,
                "state": "reconciling" if row["state"] == "submitting" else row["state"],
            }
        proposal = WebOperation.model_validate(row["proposal"])
        started = False
        # Where the browser ACTUALLY goes, as opposed to where the agent said
        # to go. A merchant's POST commonly redirects to an order page, and
        # that page -- not the agent's word for it -- is what a later
        # confirmation check is allowed to read.
        landed: list[str] = []

        def _landed(frame: Frame) -> None:
            if frame is page.main_frame and len(landed) <= MAX_LANDED_PAGES:
                landed.append(frame.url)

        page.on("framenavigated", _landed)
        try:
            if url_origin(plan.url) != proposal.origin:
                raise PermissionError("destination_mismatch")
            if (
                plan.challenge
                and plan.challenge.kind == "card_code"
                and proposal.action not in {"purchase", "subscription"}
            ):
                raise PermissionError("payment_authority_required")
            if advance and (
                not workflow_id
                or proposal.action not in {"account", "login", "application"}
                or proposal.amount_minor
                or proposal.recurring_minor
                or proposal.annual_commitment_minor
            ):
                raise PermissionError("intermediate_step_not_allowed")
            if navigate:
                await page.goto(plan.url, wait_until="domcontentloaded", timeout=30000)
            if url_origin(page.url) != proposal.origin:
                raise PermissionError("destination_changed")
            from robothor.autonomy.confirmation import observe, wait_for_confirmation

            already_confirmed = (
                await page.locator(plan.success_selector).is_visible()
                if plan.success_selector
                else bool(await observe(page, proposal.origin, proposal.action))
            )
            if already_confirmed:
                return {
                    "operation_id": operation_id,
                    "state": "reserved",
                    "reason": "confirmation_already_present",
                }
            await self._prices(page, proposal, plan, allowed_frames=allowed_frames)
            await asyncio.to_thread(self.store.check_authority, scope, operation_id, agent_id)
            from robothor.autonomy.preflight import validate_plan

            invalid = await validate_plan(self, scope, page, proposal, plan, allowed_frames)
            if invalid:
                return {
                    "operation_id": operation_id,
                    "state": "reserved",
                    "reason": "validation_required",
                    "fields": invalid,
                }
            from robothor.autonomy.terms_capture import capture_terms

            await capture_terms(
                self,
                scope,
                operation_id,
                agent_id,
                page,
                proposal.origin,
                allowed_frames,
                phase="before_input",
                material_terms=plan.material_terms,
            )
            await asyncio.to_thread(
                self.store.bind_plan, scope, operation_id, agent_id, plan.model_dump(mode="json")
            )
            challenge_locator = None
            if plan.challenge:
                challenge = plan.challenge
                root: Page | Frame | None = page
                if challenge.frame_selector:
                    if (
                        challenge.frame_origin != proposal.origin
                        and challenge.frame_origin not in allowed_frames
                    ):
                        raise PermissionError("frame_not_authorized")
                    element = await page.locator(challenge.frame_selector).element_handle()
                    root = await element.content_frame() if element else None
                    if (
                        not root
                        or not challenge.frame_origin
                        or url_origin(root.url) != challenge.frame_origin
                    ):
                        raise PermissionError("frame_origin_mismatch")
                    await self._guard_origin(page, root, challenge.frame_origin)
                assert root is not None
                challenge_locator = await self._unique(root.locator(challenge.selector))
                if verification_code is None:
                    await asyncio.to_thread(self.store.wait_for_code, scope, operation_id)
                    return {
                        "operation_id": operation_id,
                        "state": "awaiting_input",
                        "reason": "merchant_verification_required",
                        "input_path": "/account/autonomy",
                    }
                pattern = r"\d{3,4}" if challenge.kind == "card_code" else r"\d{6}"
                if not re.fullmatch(pattern, verification_code):
                    raise ValueError("invalid_verification_code")
            # Keep the actual document origin fixed across decryption and fill.
            await self._guard_origin(page, page.main_frame, proposal.origin)
            # Claim once, before any credential fill can trigger site scripts.
            await asyncio.to_thread(
                self.store.begin_submit, scope, operation_id, agent_id, workflow_id=workflow_id
            )
            started = True
            for binding in plan.fields:
                if binding.kind == "payment_card" and proposal.action not in (
                    "purchase",
                    "subscription",
                ):
                    raise PermissionError("payment_authority_required")
                await self._field(scope, page, binding, proposal.origin, allowed_frames)
            if challenge_locator:
                self.protected_values.add(verification_code or "")
                self._used_transient_code = True
                await challenge_locator.fill(verification_code or "", timeout=15000)
            for selector in plan.check_selectors:
                await (await self._unique(page.locator(selector))).check(timeout=15000)
            if url_origin(page.url) != proposal.origin:
                raise PermissionError("destination_changed")
            await self._prices(page, proposal, plan, allowed_frames=allowed_frames)
            await capture_terms(
                self,
                scope,
                operation_id,
                agent_id,
                page,
                proposal.origin,
                allowed_frames,
                phase="before_submit",
                material_terms=plan.material_terms,
            )
            # Revalidate revocation immediately before the irreversible click.
            await asyncio.to_thread(self.store.check_authority, scope, operation_id, agent_id)
            confirmed_before_click = (
                await page.locator(plan.success_selector).is_visible()
                if plan.success_selector
                else bool(await observe(page, proposal.origin, proposal.action))
            )
            if confirmed_before_click:
                raise ValueError("confirmation_before_submit")
            if workflow_id:
                from robothor.autonomy.workflows.outcome import submit_and_observe

                transition = await submit_and_observe(
                    self, page, proposal, plan, allowed_frames, advance=advance
                )
                if transition["kind"] in {"step", "validation"}:
                    from robothor.autonomy.workflows.store import WorkflowStore

                    validation = transition["kind"] == "validation"
                    await asyncio.to_thread(
                        WorkflowStore(self.store).checkpoint,
                        scope,
                        agent_id,
                        workflow_id,
                        rejected=validation,
                    )
                    return {
                        "operation_id": operation_id,
                        "state": "reserved",
                        "reason": "server_validation_required"
                        if validation
                        else "workflow_step_completed",
                        **(
                            {"fields": transition["fields"]}
                            if validation
                            else transition["inspection"]
                        ),
                    }
                confirmation_evidence = transition["evidence"]
            else:
                await (await self._unique(page.locator(plan.submit_selector))).click(timeout=15000)
                if plan.success_selector:
                    confirmation = page.locator(plan.success_selector)
                    await confirmation.wait_for(state="visible", timeout=30000)
                    text = await (await self._unique(confirmation)).inner_text()
                    if not plan.success_text or plan.success_text not in text:
                        raise ValueError("confirmation_missing")
                    confirmation_evidence = {
                        "confirmation_sha256": self._confirmation_digest(text, plan)
                    }
                else:
                    confirmation_evidence = await wait_for_confirmation(
                        page, proposal.origin, proposal.action
                    )
            if url_origin(page.url) != proposal.origin:
                raise PermissionError("confirmation_origin_changed")
            await self._record_landings(scope, operation_id, landed, page)
            evidence = {
                "origin": proposal.origin,
                **confirmation_evidence,
                "verified_at": datetime.now(UTC).isoformat(),
                "kind": "merchant_confirmation",
            }
            await asyncio.to_thread(self.store.finish, scope, operation_id, "completed", evidence)
            from robothor.autonomy.receipt_capture import capture_receipt

            receipt = await capture_receipt(self, scope, operation_id, agent_id, page, plan=plan)
            # Save authentication after confirmed completion; no cookie value
            # leaves the broker. Failure to save must not undo a completed purchase.
            # A merchant can copy a verification code into cookies/localStorage.
            # Never persist browser storage after payment or transient-code entry.
            if (
                proposal.action in {"purchase", "subscription"}
                or plan.challenge
                or self._used_transient_code
            ):
                return {
                    "operation_id": operation_id,
                    "state": "completed",
                    "evidence": evidence,
                    "session_resource_id": None,
                    **({"receipt": receipt} if receipt is not None else {}),
                }
            session_ref = None
            try:
                storage = await page.context.storage_state()
                saved = await asyncio.to_thread(
                    self.store.put_resource,
                    scope,
                    ResourceInput(
                        kind="browser_session",
                        label="Website session",
                        origin=proposal.origin,
                        payload=SecretStr(json.dumps(storage)),
                    ),
                    source="broker_session",
                )
                session_ref = saved["id"]
            except Exception:
                session_ref = None
            return {
                "operation_id": operation_id,
                "state": "completed",
                "evidence": evidence,
                "session_resource_id": session_ref,
            }
        except Exception as error:
            # Playwright exceptions often quote field contents. Never propagate
            # their text, stack locals, URL queries, or HTML to the parent.
            state = "reconciling" if started else "reserved"
            # A durable 'submitting' row is already a reconciliation marker.
            with contextlib.suppress(Exception):
                if started:
                    await self._record_landings(scope, operation_id, landed, page)
                    await asyncio.to_thread(self.store.finish, scope, operation_id, state)
            return {
                "operation_id": operation_id,
                "state": state,
                "reason": "external_result_uncertain"
                if started
                else (
                    "material_terms_unavailable"
                    if isinstance(error, MaterialTermsUnavailableError)
                    else "preflight_failed"
                ),
            }
        finally:
            with contextlib.suppress(Exception):
                page.remove_listener("framenavigated", _landed)
