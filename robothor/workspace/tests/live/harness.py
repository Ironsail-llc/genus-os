"""The live Microsoft 365 smoke suite's safety harness. Pure: nothing here opens a socket.

Three layers keep the live suite from ever touching the wrong tenant or mailbox:

1. **Skip unless asked.** :func:`skip_reason` returns a reason (so the module
   is collected and skipped) unless ``GENUS_LIVE_M365=1`` AND a credential
   source is configured. CI never sets either.
2. **Tenant allowlist.** :func:`require_allowed_tenant` refuses -- fails the
   run, before any request -- unless the directory the credential belongs to
   is listed in ``GENUS_LIVE_M365_TENANTS``. After the first token,
   :func:`require_allowed_token` checks the token's own ``tid`` claim too, so
   a domain-name tenant id cannot resolve to an unlisted directory.
3. **Mailbox fence.** :class:`FencedTransport` sits under every Graph client
   the suite builds. It refuses, before the request leaves, any host other
   than Entra and Graph, any path outside ``/users/{assistant|owner}/...``
   (the canary may only be READ, which is how its 403 is proven), and any
   write whose recipients or attendees include anyone but the assistant and
   the owner.

:class:`CapturingTransport` (``GENUS_LIVE_M365_CAPTURE=<dir>``) writes each
exchange, scrubbed by :class:`~robothor.workspace.microsoft.capture.Scrubber`,
for promotion into fixtures with ``scripts/m365_capture_to_fixtures.py``.

Environment (never committed, never logged):

========================================  ==========================================
``GENUS_LIVE_M365``                        ``1`` to run at all
``GENUS_LIVE_M365_TENANTS``                comma-separated directory ids allowed
``GENUS_LIVE_M365_PLATFORM_TENANT``        read the app credential from this platform
                                           tenant's vault (``graph_client_from_vault``)
``GENUS_LIVE_M365_TENANT_ID``              or: the directory id ...
``GENUS_LIVE_M365_CLIENT_ID``              ... the application (client) id ...
``GENUS_LIVE_M365_CERT_PATH``              ... and a PEM holding the certificate
``GENUS_LIVE_M365_KEY_PATH``               (its private key, when not in the same PEM)
``GENUS_LIVE_M365_ASSISTANT``              the assistant test mailbox
``GENUS_LIVE_M365_OWNER``                  the owner test mailbox
``GENUS_LIVE_M365_CANARY``                 the canary mailbox the app must NOT read
``GENUS_LIVE_M365_CAPTURE``                optional: directory for scrubbed captures
========================================  ==========================================
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import email.policy
import email.utils
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import unquote

import httpx

from robothor.workspace.microsoft.capture import TAG_PREFIX

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from robothor.workspace.microsoft.capture import Scrubber

__all__ = [
    "ENV_ENABLE",
    "ENV_TENANTS",
    "CapturingTransport",
    "FenceViolation",
    "FencedTransport",
    "LiveConfig",
    "LiveGuardError",
    "allowed_tenants",
    "load_config",
    "new_run_tag",
    "poll",
    "require_allowed_tenant",
    "require_allowed_token",
    "skip_reason",
]

ENV_ENABLE = "GENUS_LIVE_M365"
ENV_TENANTS = "GENUS_LIVE_M365_TENANTS"
ENV_PLATFORM_TENANT = "GENUS_LIVE_M365_PLATFORM_TENANT"
ENV_TENANT_ID = "GENUS_LIVE_M365_TENANT_ID"
ENV_CLIENT_ID = "GENUS_LIVE_M365_CLIENT_ID"
ENV_CERT_PATH = "GENUS_LIVE_M365_CERT_PATH"
ENV_KEY_PATH = "GENUS_LIVE_M365_KEY_PATH"
ENV_ASSISTANT = "GENUS_LIVE_M365_ASSISTANT"
ENV_OWNER = "GENUS_LIVE_M365_OWNER"
ENV_CANARY = "GENUS_LIVE_M365_CANARY"
ENV_CAPTURE = "GENUS_LIVE_M365_CAPTURE"

GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

T = TypeVar("T")


class LiveGuardError(RuntimeError):
    """The live suite was asked to run somewhere it must not. Never a skip."""


class FenceViolation(LiveGuardError):  # noqa: N818 - reads as the event it is
    """A request would have left the fence. It was NOT sent."""


# ── 1. skip unless asked ─────────────────────────────────────────────────────


def skip_reason(env: Mapping[str, str] | None = None) -> str | None:
    """Why the live suite does not run here, or ``None`` when it was asked to.

    Only the opt-in and the presence of a credential source decide a SKIP.
    Everything after that (allowlist, mailboxes, readable files) is a
    refusal, raised by :func:`load_config`, because an operator who switched
    the suite on must hear why it did not run.
    """
    env = os.environ if env is None else env
    if env.get(ENV_ENABLE, "").strip() != "1":
        return f"live Microsoft 365 smoke suite: set {ENV_ENABLE}=1 to run it against a dev tenant"
    if env.get(ENV_PLATFORM_TENANT, "").strip():
        return None
    if all(env.get(name, "").strip() for name in (ENV_TENANT_ID, ENV_CLIENT_ID, ENV_CERT_PATH)):
        return None
    return (
        "live Microsoft 365 smoke suite: no credentials -- set "
        f"{ENV_PLATFORM_TENANT} (vault) or {ENV_TENANT_ID}, {ENV_CLIENT_ID} and {ENV_CERT_PATH}"
    )


# ── 2. the tenant allowlist ──────────────────────────────────────────────────


def allowed_tenants(env: Mapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if env is None else env
    raw = env.get(ENV_TENANTS, "")
    return frozenset(part.strip().lower() for part in re.split(r"[,\s]+", raw) if part.strip())


def require_allowed_tenant(tenant_id: str, env: Mapping[str, str] | None = None) -> str:
    """``tenant_id`` when it is explicitly allowlisted; :class:`LiveGuardError` otherwise."""
    allowed = allowed_tenants(env)
    if not allowed:
        raise LiveGuardError(
            f"refusing to run: {ENV_TENANTS} is empty. List the dev tenant's directory id "
            "explicitly; the live suite never runs against an unlisted tenant"
        )
    if "*" in allowed:
        raise LiveGuardError(f"refusing to run: {ENV_TENANTS} does not accept a wildcard")
    tenant = (tenant_id or "").strip().lower()
    if not tenant or tenant not in allowed:
        raise LiveGuardError(
            f"refusing to run: the credential's tenant is not in {ENV_TENANTS} "
            "(is this a client's production tenant?)"
        )
    return tenant


def require_allowed_token(access_token: str, env: Mapping[str, str] | None = None) -> str:
    """The token's own directory (``tid``) must be allowlisted too. Never logs the token."""
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        tid = str(claims.get("tid") or "")
    except (IndexError, ValueError, AttributeError):
        raise LiveGuardError("refusing to run: the access token carries no readable tid") from None
    if not _GUID.match(tid):
        raise LiveGuardError("refusing to run: the access token carries no directory id")
    return require_allowed_tenant(tid, env)


# ── configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiveConfig:
    """Where the live suite may go. ``repr`` never shows a path to key material."""

    assistant: str
    owner: str
    canary: str
    allowed: frozenset[str]
    platform_tenant: str = ""
    tenant_id: str = ""
    client_id: str = ""
    cert_path: str = field(default="", repr=False)
    key_path: str = field(default="", repr=False)
    capture_dir: str = ""

    @property
    def writable(self) -> frozenset[str]:
        """The only mailboxes the suite may write to or address mail and invites to."""
        return frozenset({self.assistant, self.owner})

    @property
    def readable(self) -> frozenset[str]:
        return frozenset({self.assistant, self.owner, self.canary})

    @property
    def uses_vault(self) -> bool:
        return bool(self.platform_tenant)

    def placeholders(self) -> dict[str, str]:
        """Real address -> what a capture calls it."""
        return {
            self.assistant: "assistant@tenant.example",
            self.owner: "owner@tenant.example",
            self.canary: "canary@tenant.example",
        }


def _mailbox(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip().lower()
    if not value:
        raise LiveGuardError(f"refusing to run: {name} is not set")
    if not _EMAIL.match(value):
        raise LiveGuardError(f"refusing to run: {name} is not an email address")
    return value


def load_config(env: Mapping[str, str] | None = None) -> LiveConfig:
    """The validated configuration. Raises :class:`LiveGuardError`; reads no network.

    In env mode the directory id is allowlist-checked here. In vault mode it
    is checked as soon as the vault row is read (``conftest``), still before
    any request.
    """
    env = os.environ if env is None else env
    reason = skip_reason(env)
    if reason is not None:
        raise LiveGuardError(reason)
    allowed = allowed_tenants(env)
    assistant = _mailbox(env, ENV_ASSISTANT)
    owner = _mailbox(env, ENV_OWNER)
    canary = _mailbox(env, ENV_CANARY)
    if len({assistant, owner, canary}) != 3:
        raise LiveGuardError(
            "refusing to run: the assistant, owner and canary must be three different mailboxes"
        )
    platform_tenant = env.get(ENV_PLATFORM_TENANT, "").strip()
    tenant_id = env.get(ENV_TENANT_ID, "").strip()
    client_id = env.get(ENV_CLIENT_ID, "").strip()
    cert_path = env.get(ENV_CERT_PATH, "").strip()
    key_path = env.get(ENV_KEY_PATH, "").strip()
    if not allowed:
        require_allowed_tenant("", env)  # raises: empty allowlist
    if not platform_tenant:
        require_allowed_tenant(tenant_id, env)
        if not Path(cert_path).is_file():
            raise LiveGuardError(f"refusing to run: {ENV_CERT_PATH} is not a readable file")
        if key_path and not Path(key_path).is_file():
            raise LiveGuardError(f"refusing to run: {ENV_KEY_PATH} is not a readable file")
    return LiveConfig(
        assistant=assistant,
        owner=owner,
        canary=canary,
        allowed=allowed,
        platform_tenant=platform_tenant,
        tenant_id=tenant_id,
        client_id=client_id,
        cert_path=cert_path,
        key_path=key_path,
        capture_dir=env.get(ENV_CAPTURE, "").strip(),
    )


def new_run_tag(now: datetime | None = None) -> str:
    """A unique tag for one run; every subject the suite writes carries it."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    return f"{TAG_PREFIX}{stamp}-{secrets.token_hex(3)}"


# ── 3. the mailbox fence ─────────────────────────────────────────────────────


_USERS = re.compile(r"^/(?:v1\.0|beta)/users/(?P<who>[^/]+)(?P<rest>/.*)?$")
_RECIPIENT_KEYS = ("toRecipients", "ccRecipients", "bccRecipients", "replyTo", "attendees")


def _json_addresses(body: Any) -> list[str]:
    """Every recipient / attendee address in a JSON write body."""
    found: list[str] = []
    if isinstance(body, dict):
        for key in _RECIPIENT_KEYS:
            for entry in body.get(key) or []:
                address = ((entry or {}).get("emailAddress") or {}).get("address")
                if address:
                    found.append(str(address).strip().lower())
        for key in ("message",):  # sendMail / reply payloads nest a message
            found.extend(_json_addresses(body.get(key)))
    return found


def _mime_addresses(content: bytes) -> list[str]:
    """Every To/Cc/Bcc address in a base64 MIME upload (Graph's draft-from-MIME)."""
    try:
        mime = base64.b64decode(content, validate=False)
    except ValueError:
        return []
    parsed = email.message_from_bytes(mime, policy=email.policy.default)
    pairs = email.utils.getaddresses(
        [str(v) for h in ("To", "Cc", "Bcc") for v in parsed.get_all(h, [])]
    )
    return [address.strip().lower() for _, address in pairs if address]


class FencedTransport(httpx.AsyncBaseTransport):
    """Refuses, before sending, any request outside the configured test mailboxes."""

    def __init__(self, inner: httpx.AsyncBaseTransport, config: LiveConfig) -> None:
        self._inner = inner
        self._config = config
        #: Every request that passed the fence, as ``(method, path)``.
        self.passed: list[tuple[str, str]] = []
        #: A user object id Graph itself put in a nextLink/deltaLink it
        #: returned for one of the test mailboxes -> that mailbox.
        self.aliases: dict[str, str] = {}

    def check(self, request: httpx.Request) -> str:
        """The test mailbox ``request`` addresses ("" for Entra), or :class:`FenceViolation`."""
        if request.url.scheme != "https":
            raise FenceViolation("refused: not https")
        host = request.url.host
        if host == LOGIN_HOST:
            if request.method != "POST" or not request.url.path.endswith("/oauth2/v2.0/token"):
                raise FenceViolation("refused: only the token endpoint may be called on Entra")
            directory = request.url.path.strip("/").split("/", 1)[0]
            if directory.lower() not in self._config.allowed:
                raise FenceViolation("refused: a token request for a directory not allowlisted")
            return ""
        if host != GRAPH_HOST:
            raise FenceViolation(f"refused: host {host!r} is neither Graph nor Entra")
        match = _USERS.match(request.url.path)
        if match is None:
            raise FenceViolation("refused: only /users/{test mailbox}/... is in scope")
        who = unquote(match.group("who")).strip().lower()
        who = self.aliases.get(who, who)
        if who not in self._config.readable:
            raise FenceViolation("refused: a mailbox that is not one of the configured test ones")
        if who == self._config.canary and request.method != "GET":
            raise FenceViolation("refused: the canary mailbox may only be read (to prove a 403)")
        if request.method in ("POST", "PATCH", "PUT"):
            self._check_recipients(request)
        return who

    def learn(self, mailbox: str, body: Any) -> None:
        """Graph may address a mailbox by its object id in the links it hands back.

        Only an alias Graph returned in answer to a request for an allowed
        mailbox is learned, and only as that same mailbox.
        """
        if not mailbox or not isinstance(body, dict):
            return
        for key in ("@odata.nextLink", "@odata.deltaLink"):
            link = body.get(key)
            if not isinstance(link, str):
                continue
            match = _USERS.match(httpx.URL(link).path)
            if match is None:
                continue
            alias = unquote(match.group("who")).strip().lower()
            if alias not in self._config.readable:
                self.aliases.setdefault(alias, mailbox)

    def _check_recipients(self, request: httpx.Request) -> None:
        content = request.content or b""
        if not content:
            return
        ctype = request.headers.get("content-type", "").lower()
        if "json" in ctype:
            try:
                addresses = _json_addresses(json.loads(content))
            except ValueError:
                raise FenceViolation("refused: an unreadable JSON write body") from None
        else:
            addresses = _mime_addresses(content)
        outside = sorted(set(addresses) - self._config.writable)
        if outside:
            raise FenceViolation(
                f"refused: {len(outside)} recipient(s) outside the assistant and owner mailboxes"
            )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        mailbox = self.check(request)
        self.passed.append((request.method, request.url.path))
        response = await self._inner.handle_async_request(request)
        if not mailbox or "json" not in response.headers.get("content-type", ""):
            return response
        body = await response.aread()
        with contextlib.suppress(ValueError):
            self.learn(mailbox, json.loads(body))
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# ── capture ──────────────────────────────────────────────────────────────────


class CapturingTransport(httpx.AsyncBaseTransport):
    """Writes every exchange, scrubbed, as ``<dir>/<label>/<seq>.json``.

    Sits INSIDE the fence (``FencedTransport(CapturingTransport(real))``), so
    a refused request is never recorded as sent, and scrubs before anything
    touches the disk.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        directory: str | Path,
        scrubber: Scrubber,
        label: Callable[[], str] = lambda: "session",
    ) -> None:
        self._inner = inner
        self._dir = Path(directory)
        self._scrubber = scrubber
        self._label = label
        self._seq = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        record = self._scrubber.exchange(
            method=request.method,
            url=str(request.url),
            request_headers=dict(request.headers),
            request_body=request.content or b"",
            status=response.status_code,
            response_headers=dict(response.headers),
            response_body=body,
        )
        self._seq += 1
        record["seq"] = self._seq
        record["test"] = self._label()
        folder = self._dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", record["test"])[:120]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{self._seq:05d}.json").write_text(json.dumps(record, indent=2, sort_keys=True))
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def write_sample(directory: str | Path, name: str, payload: Any, scrubber: Scrubber) -> Path:
    """A scrubbed observation (e.g. an Authentication-Results sample) next to the captures."""
    folder = Path(directory) / "samples"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.json"
    path.write_text(json.dumps(scrubber.value(payload), indent=2, sort_keys=True))
    return path


# ── waiting on Exchange ──────────────────────────────────────────────────────


async def poll(
    probe: Callable[[], Awaitable[T | None]],
    *,
    timeout: float = 180.0,
    interval: float = 5.0,
    what: str = "the condition",
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``probe`` until it returns something truthy; ``AssertionError`` on timeout.

    Exchange delivers mail and invitation updates asynchronously, so the live
    suite waits for what it observes rather than assuming it.
    """
    deadline = clock() + timeout
    while True:
        result = await probe()
        if result:
            return result
        if clock() >= deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")
        await sleep(interval)


def addresses_of(entries: Iterable[Any]) -> list[str]:
    """Lower-cased addresses of Graph recipients or attendees."""
    out = []
    for entry in entries or []:
        address = ((entry or {}).get("emailAddress") or {}).get("address")
        if address:
            out.append(str(address).lower())
    return out
