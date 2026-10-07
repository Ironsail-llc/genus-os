"""An in-memory Microsoft 365 tenant behind an ``httpx.MockTransport``.

One object plays both halves of the conversation the Graph transport has:
the Entra token endpoint (``login.microsoftonline.com``) and Microsoft Graph
(``graph.microsoft.com``). Tests hand :meth:`FakeGraphTenant.transport` to both
the token source and the :class:`~robothor.workspace.microsoft.graph.GraphClient`
and then read back what was asked of it.

Built to be extended rather than rewritten by the PRs that follow (mail,
calendar, delta ingestion):

* **state** lives in :attr:`FakeGraphTenant.mailboxes`, one
  :class:`FakeMailbox` per address, each a bag of named collections;
* **behaviour** lives in a handler registry: ``tenant.route("GET",
  r"/users/(?P<mailbox>[^/]+)/messages")`` registers an ``async`` handler that
  receives ``(tenant, request, match)``. Later routes win over earlier ones,
  so a test can override a built-in for one scenario;
* **faults** are scripted with :meth:`FakeGraphTenant.fail` -- the next N
  matching requests answer with a chosen status/headers instead of reaching a
  handler.

Errors are answered in Graph's own envelope (``{"error": {"code", "message"}}``)
and Entra's (``{"error", "error_description"}``), including Entra's habit of
quoting the rejected secret back inside ``error_description`` -- which is what
the safe-error tests rely on.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import jwt

TENANT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GRAPH_ORIGIN = "https://graph.microsoft.com"
GRAPH_BASE = f"{GRAPH_ORIGIN}/v1.0"
LOGIN_HOST = "login.microsoftonline.com"

Handler = Callable[["FakeGraphTenant", httpx.Request, "re.Match[str]"], Awaitable[httpx.Response]]


def graph_error(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    """A response in Graph's error envelope."""
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": message}},
        headers=headers or {},
    )


@dataclass
class FakeMailbox:
    """One mailbox's state: named collections of JSON-shaped items."""

    address: str
    collections: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.collections["messages"]


@dataclass
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler


@dataclass
class _Fault:
    method: str
    pattern: re.Pattern[str]
    status: int
    headers: dict[str, str]
    body: dict[str, Any] | None
    remaining: int
    raise_exc: type[Exception] | None = None


class FakeGraphTenant:
    """A Microsoft 365 tenant small enough to reason about in a test."""

    def __init__(
        self,
        *,
        tenant_id: str = TENANT_ID,
        client_id: str = CLIENT_ID,
        client_secret: str | None = None,
        certificate_pem: str | None = None,
        token_lifetime: int = 3600,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.certificate_pem = certificate_pem
        self.token_lifetime = token_lifetime
        self.mailboxes: dict[str, FakeMailbox] = {}
        #: Every Graph request, in arrival order (token requests excluded).
        self.requests: list[httpx.Request] = []
        #: The parsed form of every token request.
        self.token_requests: list[dict[str, str]] = []
        #: Decoded client-assertion claims and headers, one per assertion grant.
        self.assertions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.issued_tokens: list[str] = []
        #: Seconds each built-in handler waits before answering; lets a test
        #: hold requests in flight to observe concurrency.
        self.latency = 0.0
        self.inflight: dict[str, int] = defaultdict(int)
        self.max_inflight: dict[str, int] = defaultdict(int)
        #: When set, nextLinks point at this origin instead of Graph's.
        self.next_link_origin: str | None = None
        self._routes: list[_Route] = []
        self._faults: list[_Fault] = []
        self._token_counter = itertools.count(1)
        self._install_builtin_routes()

    # ── public API ──────────────────────────────────────────────────────

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._dispatch)

    def mailbox(self, address: str) -> FakeMailbox:
        key = address.lower()
        if key not in self.mailboxes:
            self.mailboxes[key] = FakeMailbox(address=key)
        return self.mailboxes[key]

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        """Register a handler for ``method`` + a regex over the path after ``/v1.0``."""

        def register(handler: Handler) -> Handler:
            self._routes.append(_Route(method.upper(), re.compile(f"^{pattern}$"), handler))
            return handler

        return register

    def fail(
        self,
        method: str,
        pattern: str,
        status: int = 500,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        times: int = 1,
        raise_exc: type[Exception] | None = None,
    ) -> None:
        """Answer the next ``times`` matching Graph requests with a fault."""
        self._faults.append(
            _Fault(
                method.upper(),
                re.compile(f"^{pattern}$"),
                status,
                headers or {},
                body,
                times,
                raise_exc,
            )
        )

    def requests_matching(self, method: str, pattern: str) -> list[httpx.Request]:
        rx = re.compile(f"^{pattern}$")
        return [r for r in self.requests if r.method == method.upper() and rx.match(_graph_path(r))]

    # ── dispatch ────────────────────────────────────────────────────────

    async def _dispatch(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == LOGIN_HOST:
            return self._token_endpoint(request)
        self.requests.append(request)
        if request.url.host != httpx.URL(GRAPH_ORIGIN).host:
            return httpx.Response(404, text="unknown host")
        path = _graph_path(request)
        for fault in self._faults:
            if fault.remaining > 0 and fault.method == request.method and fault.pattern.match(path):
                fault.remaining -= 1
                if fault.raise_exc is not None:
                    raise fault.raise_exc("scripted transport failure", request=request)
                if fault.body is not None:
                    return httpx.Response(fault.status, json=fault.body, headers=fault.headers)
                return graph_error(fault.status, f"Fault{fault.status}", "scripted", fault.headers)
        if not self._authorised(request):
            return graph_error(401, "InvalidAuthenticationToken", "Access token is empty.")
        for route in reversed(self._routes):
            if route.method != request.method:
                continue
            match = route.pattern.match(path)
            if match:
                return await route.handler(self, request, match)
        return graph_error(400, "BadRequest", "Resource not found for the segment.")

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.issued_tokens

    # ── Entra token endpoint ────────────────────────────────────────────

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        expected_path = f"/{self.tenant_id}/oauth2/v2.0/token"
        if request.method != "POST" or request.url.path != expected_path:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_request",
                    "error_description": "AADSTS90002: Tenant not found.",
                },
            )
        if form.get("client_id") != self.client_id or form.get("grant_type") != (
            "client_credentials"
        ):
            return httpx.Response(
                400,
                json={
                    "error": "unauthorized_client",
                    "error_description": "AADSTS700016: Application not found.",
                },
            )
        assertion = form.get("client_assertion")
        if assertion:
            if not self.certificate_pem:
                return self._invalid_client("AADSTS700027: no certificate registered.")
            from cryptography import x509

            cert = x509.load_pem_x509_certificate(self.certificate_pem.encode())
            header = jwt.get_unverified_header(assertion)
            try:
                claims = jwt.decode(
                    assertion,
                    cert.public_key(),  # type: ignore[arg-type]
                    algorithms=["RS256", "PS256"],
                    audience=str(request.url),
                    issuer=self.client_id,
                )
            except jwt.PyJWTError as exc:
                return self._invalid_client(f"AADSTS700027: assertion invalid ({exc}).")
            self.assertions.append((claims, header))
        elif form.get("client_secret") != self.client_secret or not self.client_secret:
            return self._invalid_client(
                f"AADSTS7000215: Invalid client secret provided. [{form.get('client_secret', '')}]"
            )
        token = f"fake-graph-token-{next(self._token_counter)}"
        self.issued_tokens.append(token)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": self.token_lifetime,
                "access_token": token,
            },
        )

    @staticmethod
    def _invalid_client(description: str) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "invalid_client", "error_description": description}
        )

    # ── built-in Graph routes ───────────────────────────────────────────

    def _install_builtin_routes(self) -> None:
        mailbox_rx = r"/users/(?P<mailbox>[^/]+)"

        @self.route("GET", mailbox_rx + r"/messages")
        async def list_messages(tenant, request, match):
            box = tenant.mailbox(unquote(match["mailbox"]))
            async with tenant._occupy(box.address):
                params = request.url.params
                top = int(params.get("$top", "10"))
                skip = int(params.get("$skiptoken", "0"))
                page = box.messages[skip : skip + top]
                body: dict[str, Any] = {"value": page}
                if skip + top < len(box.messages):
                    origin = tenant.next_link_origin or GRAPH_ORIGIN
                    body["@odata.nextLink"] = (
                        f"{origin}/v1.0/users/{match['mailbox']}/messages"
                        f"?$top={top}&$skiptoken={skip + top}"
                    )
                return httpx.Response(200, json=body)

        @self.route("GET", mailbox_rx + r"/messages/(?P<id>[^/]+)")
        async def get_message(tenant, request, match):
            box = tenant.mailbox(unquote(match["mailbox"]))
            async with tenant._occupy(box.address):
                for message in box.messages:
                    if message.get("id") == match["id"]:
                        return httpx.Response(200, json=message)
                return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")

        @self.route("POST", mailbox_rx + r"/messages")
        async def create_message(tenant, request, match):
            box = tenant.mailbox(unquote(match["mailbox"]))
            async with tenant._occupy(box.address):
                item = json.loads(request.content or b"{}")
                item.setdefault("id", f"msg-{len(box.messages) + 1}")
                box.messages.append(item)
                return httpx.Response(201, json=item)

        @self.route("PATCH", mailbox_rx + r"/messages/(?P<id>[^/]+)")
        async def update_message(tenant, request, match):
            box = tenant.mailbox(unquote(match["mailbox"]))
            async with tenant._occupy(box.address):
                for message in box.messages:
                    if message.get("id") == match["id"]:
                        etag = request.headers.get("if-match")
                        if etag and etag != message.get("@odata.etag"):
                            return graph_error(
                                412, "ErrorIrresolvableConflict", "The change key is stale."
                            )
                        message.update(json.loads(request.content or b"{}"))
                        return httpx.Response(200, json=message)
                return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")

        @self.route("DELETE", mailbox_rx + r"/messages/(?P<id>[^/]+)")
        async def delete_message(tenant, request, match):
            box = tenant.mailbox(unquote(match["mailbox"]))
            async with tenant._occupy(box.address):
                before = len(box.messages)
                box.messages[:] = [m for m in box.messages if m.get("id") != match["id"]]
                if len(box.messages) == before:
                    return graph_error(
                        404, "ErrorItemNotFound", "The specified object was not found."
                    )
                return httpx.Response(204)

    def _occupy(self, mailbox: str) -> _Occupancy:
        return _Occupancy(self, mailbox)


class _Occupancy:
    """Counts a request as in flight against a mailbox for its duration."""

    def __init__(self, tenant: FakeGraphTenant, mailbox: str) -> None:
        self.tenant = tenant
        self.mailbox = mailbox

    async def __aenter__(self) -> None:
        self.tenant.inflight[self.mailbox] += 1
        self.tenant.max_inflight[self.mailbox] = max(
            self.tenant.max_inflight[self.mailbox], self.tenant.inflight[self.mailbox]
        )
        if self.tenant.latency:
            await asyncio.sleep(self.tenant.latency)

    async def __aexit__(self, *exc: object) -> None:
        self.tenant.inflight[self.mailbox] -= 1


def _graph_path(request: httpx.Request) -> str:
    """The request path below ``/v1.0``, still percent-encoded."""
    path = request.url.raw_path.decode().split("?", 1)[0]
    return path.removeprefix("/v1.0")


def make_certificate(common_name: str = "genus-test") -> tuple[str, str]:
    """A throwaway self-signed RSA certificate: ``(certificate_pem, private_key_pem)``."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem
