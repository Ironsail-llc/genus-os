"""Scrubbed captures of Microsoft Graph traffic, for promotion into test fixtures.

The live Microsoft 365 smoke suite (``robothor/workspace/tests/live/``) can
record every request/response pair it exchanges with a dev tenant
(``GENUS_LIVE_M365_CAPTURE=<dir>``). What it writes is scrubbed at write time
by :class:`Scrubber`, and ``scripts/m365_capture_to_fixtures.py`` scrubs it
again and runs :func:`find_leaks` before anything is promoted into the repo as
a :class:`~robothor.workspace.tests.fake_graph.FakeGraphTenant` fixture.

What the scrubber does to an exchange:

* **Credentials never survive.** ``Authorization``, cookies and every header
  not on a short allowlist are dropped; a JWT anywhere becomes ``<jwt>``;
  Entra's token form keeps only which assertion headers were present
  (``alg``, ``x5t``, ``x5t#S256``), never their values; ``client_secret`` and
  ``access_token`` become ``<redacted>``.
* **Identifiers are hashed** with a per-capture HMAC salt, so a message id in
  a URL and the same id in a body still match each other but not the tenant:
  Graph ids become ``id-<12 hex>``, etags ``etag-<12 hex>``, GUIDs (tenant,
  client, transaction ids) ``00000000-xxxx-...``.
* **Addresses become ``*.example``.** The three configured mailboxes map to
  ``assistant@``, ``owner@`` and ``canary@tenant.example``; every other address
  (plain or ``%40``-encoded) to ``u-<hash>@<hash>.example``.
* **Content is kept only when the suite wrote it.** Subjects, bodies and
  display names survive only when they carry the run-tag prefix
  (:data:`TAG_PREFIX`); anything else in a dev tenant's mailbox (a welcome
  mail, a calendar someone shared) is replaced by a length marker.
* **Mail headers** (``internetMessageHeaders``) keep their names and order;
  their values lose addresses, IP addresses (mapped into the TEST-NET
  ranges) and every domain that is not Microsoft's own infrastructure.

Every rule is idempotent: scrubbing a scrubbed exchange changes nothing a
reader can rely on, so the promotion script can always scrub once more.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "SCHEMA",
    "TAG_PREFIX",
    "Scrubber",
    "find_leaks",
]

#: Version of the capture file format.
SCHEMA = 1
#: Every subject, body and name the live suite writes starts with this.
TAG_PREFIX = "genus-live-"

#: Request headers worth keeping in a fixture. Everything else is dropped.
_KEEP_REQUEST_HEADERS = frozenset({"content-type", "prefer", "if-match", "accept"})
#: Response headers worth keeping in a fixture.
_KEEP_RESPONSE_HEADERS = frozenset(
    {"content-type", "preference-applied", "retry-after", "etag", "location"}
)

#: JSON keys whose string value is an opaque Graph identifier.
_ID_KEYS = frozenset(
    {
        "id",
        "conversationId",
        "parentFolderId",
        "seriesMasterId",
        "iCalUId",
        "uid",
        "changeKey",
        "conversationIndex",
        "calendarId",
        "internetMessageId",
        "transactionId",
    }
)
#: JSON keys whose value is an etag.
_ETAG_KEYS = frozenset({"@odata.etag", "etag"})
#: JSON keys holding content the suite may or may not have written.
_CONTENT_KEYS = frozenset({"content", "bodyPreview", "subject", "comment"})
#: JSON keys holding a person's or place's display name.
_NAME_KEYS = frozenset({"name", "displayName"})
#: JSON keys holding a URL that names tenant data (a link, never a token).
_URL_KEYS = frozenset({"@odata.nextLink", "@odata.deltaLink", "@odata.id", "@odata.context"})
#: JSON keys whose value is a link into Outlook on the web for this tenant.
_WEBLINK_KEYS = frozenset({"webLink", "joinUrl", "joinWebUrl"})
#: Well-known folder and calendar names that are not tenant data.
_SAFE_NAMES = frozenset(
    {
        "Inbox",
        "Sent Items",
        "Deleted Items",
        "Drafts",
        "Junk Email",
        "Archive",
        "Outbox",
        "Calendar",
        "Conversation History",
        "",
    }
)
#: Well-known folder names allowed as a URL path segment.
_SAFE_SEGMENTS = frozenset(
    {
        "inbox",
        "sentitems",
        "deleteditems",
        "drafts",
        "junkemail",
        "archive",
        "outbox",
        "calendar",
        "calendarview",
        "messages",
        "events",
        "mailfolders",
        "calendars",
        "delta",
        "instances",
        "attachments",
        "mailboxsettings",
        "timezone",
        "users",
        "v1.0",
        "beta",
        "send",
        "reply",
        "replyall",
        "createreplyall",
        "createreply",
        "move",
        "accept",
        "decline",
        "tentativelyaccept",
        "permanentdelete",
        "internetmessageheaders",
        "cancel",
        "oauth2",
        "v2.0",
        "token",
        "$metadata",
    }
)
#: Query parameters that carry a delta or paging token.
_TOKEN_PARAMS = frozenset({"$deltatoken", "$skiptoken", "deltatoken", "skiptoken"})
#: Domains of Microsoft's own infrastructure: not tenant data, kept in headers.
_INFRA_DOMAINS = (
    "outlook.com",
    "office365.com",
    "microsoft.com",
    "microsoftonline.com",
    "office.com",
    "exchangelabs.com",
    "example",
)

_EMAIL = re.compile(
    r"(?P<local>[A-Za-z0-9._+-]+)(?P<at>@|%40|%2540)(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
)
_GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_ONMICROSOFT = re.compile(r"(?i)\b[a-z0-9-]+\.(?:mail\.)?onmicrosoft\.com\b")
_DOMAIN = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
_ETAG = re.compile(r'^(W/)?"?(?P<value>[^"]*)"?$')
_SCRUBBED_ID = re.compile(r"^(?:id|etag)-[0-9a-f]{12}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9+/=_-]{16,}$")


def _is_scrubbed_guid(value: str) -> bool:
    return value.startswith("00000000-")


def _redacted(value: str) -> str:
    return f"<redacted {len(value)} chars>"


class Scrubber:
    """Scrubs one capture. Mappings are stable within one instance.

    ``salt`` keys the hashes (a fresh random salt per capture means two
    captures cannot be joined on their ids). ``mailboxes`` maps a real
    address to the placeholder it becomes (``assistant@tenant.example``).
    ``forbidden`` is any literal that must never survive (the tenant's own
    domain names, its directory id); each is replaced wherever it appears.
    """

    def __init__(
        self,
        *,
        salt: bytes,
        mailboxes: Mapping[str, str] | None = None,
        forbidden: Iterable[str] = (),
    ) -> None:
        if not salt:
            raise ValueError("a capture scrubber needs a salt")
        self._salt = salt
        self._mailboxes = {k.strip().lower(): v for k, v in (mailboxes or {}).items() if k}
        self._forbidden = sorted({f for f in forbidden if f and len(f) >= 4}, key=len, reverse=True)

    # ── primitives ──────────────────────────────────────────────────────

    def digest(self, value: str, length: int = 12) -> str:
        return hmac.new(self._salt, value.encode(), hashlib.sha256).hexdigest()[:length]

    def opaque_id(self, value: str) -> str:
        if not value or _SCRUBBED_ID.match(value):
            return value
        return f"id-{self.digest(value)}"

    def etag(self, value: str) -> str:
        match = _ETAG.match(value or "")
        if not value or match is None:
            return value
        inner = match.group("value")
        if _SCRUBBED_ID.match(inner):
            return value
        return f'{match.group(1) or ""}"etag-{self.digest(inner)}"'

    def guid(self, value: str) -> str:
        if _is_scrubbed_guid(value):
            return value
        h = self.digest(value.lower(), 24)
        return f"00000000-{h[0:4]}-{h[4:8]}-{h[8:12]}-{h[12:24]}"

    def address(self, local: str, domain: str) -> tuple[str, str]:
        real = f"{local}@{domain}".lower()
        if real in self._mailboxes:
            mapped = self._mailboxes[real]
            mapped_local, _, mapped_domain = mapped.partition("@")
            return mapped_local, mapped_domain
        if domain.lower().endswith(".example") or domain.lower() == "example.com":
            return local, domain
        return f"u-{self.digest(real, 8)}", f"{self.digest(domain.lower(), 6)}.example"

    def _email(self, match: re.Match[str]) -> str:
        local, domain = self.address(match.group("local"), match.group("domain"))
        return f"{local}{match.group('at')}{domain}"

    def _ip(self, match: re.Match[str]) -> str:
        text = match.group(0)
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return text
        if ip.version == 4:
            if ip in ipaddress.ip_network("192.0.2.0/24") or ip.is_loopback:
                return text
            return f"192.0.2.{int(self.digest(text, 4), 16) % 254 + 1}"
        if ip in ipaddress.ip_network("2001:db8::/32") or ip.is_loopback:
            return text
        return f"2001:db8::{self.digest(text, 4)}"

    def _domain(self, match: re.Match[str]) -> str:
        domain = match.group(0)
        lower = domain.lower()
        for infra in _INFRA_DOMAINS:
            if lower == infra or lower.endswith("." + infra):
                return domain
        # A version number or a header token ("8.17.1") is not a domain.
        if all(part.isdigit() for part in lower.split(".")):
            return domain
        return f"{self.digest(lower, 6)}.example"

    def text(self, value: str, *, domains: bool = False) -> str:
        """Every credential, address, GUID and IP in free text, scrubbed."""
        if not value:
            return value
        out = _JWT.sub("<jwt>", value)
        out = _BEARER.sub("Bearer <redacted>", out)
        out = _EMAIL.sub(self._email, out)
        out = _ONMICROSOFT.sub("tenant.example", out)
        for literal in self._forbidden:
            out = re.sub(re.escape(literal), "<tenant>", out, flags=re.IGNORECASE)
        out = _GUID.sub(lambda m: self.guid(m.group(0)), out)
        out = _IPV4.sub(self._ip, out)
        out = _IPV6.sub(self._ip, out)
        if domains:
            out = _DOMAIN.sub(self._domain, out)
        return out

    def content(self, value: str) -> str:
        """Kept (scrubbed) when the suite wrote it; otherwise only its length."""
        if not isinstance(value, str):
            return value
        if TAG_PREFIX in value or value.startswith("<redacted"):
            return self.text(value)
        return _redacted(value) if value else value

    def name(self, value: str) -> str:
        if not isinstance(value, str) or value in _SAFE_NAMES:
            return value
        if TAG_PREFIX in value or value.startswith("<redacted"):
            return self.text(value)
        # A display name the tenant chose; an address-shaped one is mapped.
        if _EMAIL.fullmatch(value):
            return self.text(value)
        return _redacted(value)

    # ── URLs ────────────────────────────────────────────────────────────

    def path(self, path: str) -> str:
        segments = []
        for raw in path.split("/"):
            segment = unquote(raw)
            lower = segment.lower()
            if not segment or lower in _SAFE_SEGMENTS:
                segments.append(raw)
            elif _EMAIL.fullmatch(segment):
                segments.append(quote(self.text(segment), safe="@."))
            elif _GUID.fullmatch(segment):
                segments.append(self.guid(segment))
            elif segment.startswith("users('") or segment.startswith("$metadata"):
                segments.append(quote(self.text(segment), safe="@.()'#$,=/"))
            elif _OPAQUE.match(segment) and not _SCRUBBED_ID.match(segment):
                segments.append(self.opaque_id(segment))
            else:
                segments.append(quote(self.text(segment), safe="@.()'$,=-_"))
        return "/".join(segments)

    def url(self, url: str) -> str:
        if not url:
            return url
        parts = urlsplit(url)
        if not parts.scheme:
            return self.text(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in _TOKEN_PARAMS:
                query.append((key, "<token>"))
            else:
                query.append((key, self.text(value)))
        host = parts.netloc
        if not any(host == d or host.endswith("." + d) for d in _INFRA_DOMAINS):
            host = "host.example"
        path = self.path(parts.path)
        fragment = self.text(unquote(parts.fragment)) if parts.fragment else ""
        return urlunsplit((parts.scheme, host, path, urlencode(query, safe="$'(),"), fragment))

    # ── JSON ────────────────────────────────────────────────────────────

    def value(self, obj: Any, key: str | None = None) -> Any:
        """A JSON value, recursively scrubbed. ``key`` is the key it sits under."""
        if isinstance(obj, dict):
            return self._dict(obj)
        if isinstance(obj, list):
            if key == "internetMessageHeaders":
                return [self._header(item) for item in obj]
            return [self.value(item, key) for item in obj]
        if not isinstance(obj, str):
            return obj
        if key in _ID_KEYS:
            return self.opaque_id(obj)
        if key in _ETAG_KEYS:
            return self.etag(obj)
        if key in _URL_KEYS:
            return self.url(obj)
        if key in _WEBLINK_KEYS:
            return "https://outlook.example/<redacted>" if obj else obj
        if key in _CONTENT_KEYS:
            return self.content(obj)
        if key in _NAME_KEYS:
            return self.name(obj)
        if key in ("access_token", "refresh_token", "id_token", "client_secret"):
            return "<redacted>"
        return self.text(obj)

    def _dict(self, obj: dict[str, Any]) -> dict[str, Any]:
        out = {k: self.value(v, k) for k, v in obj.items()}
        # An emailAddress's name follows its (scrubbed) address, so a fixture
        # still reads "assistant" for the assistant.
        if isinstance(obj.get("address"), str) and "name" in obj:
            local = str(out["address"]).partition("@")[0]
            out["name"] = local
        return out

    def _header(self, item: Any) -> Any:
        if not isinstance(item, dict):
            return self.value(item)
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name.lower() in ("subject", "thread-topic"):
            scrubbed = self.content(value)
        elif name.lower() in ("message-id", "in-reply-to", "references", "thread-index"):
            scrubbed = " ".join(self.opaque_id(part) for part in value.split()) if value else value
        else:
            scrubbed = self.text(value, domains=True)
        return {"name": name, "value": scrubbed}

    # ── whole exchanges ─────────────────────────────────────────────────

    def headers(self, headers: Mapping[str, str], *, response: bool) -> dict[str, str]:
        keep = _KEEP_RESPONSE_HEADERS if response else _KEEP_REQUEST_HEADERS
        out: dict[str, str] = {}
        for name, value in headers.items():
            lower = name.lower()
            if lower not in keep:
                continue
            if lower in ("if-match", "etag"):
                out[lower] = self.etag(value)
            elif lower == "location":
                out[lower] = self.url(value)
            else:
                out[lower] = self.text(value)
        return out

    def token_form(self, form: Mapping[str, str]) -> dict[str, Any]:
        """Entra's token request: which assertion it was, never the assertion."""
        out: dict[str, Any] = {}
        for key, value in form.items():
            if key == "client_assertion":
                out[key] = "<jwt>"
                out["client_assertion_header"] = _assertion_header_shape(value)
            elif key == "client_secret":
                out[key] = "<redacted>"
            elif key == "client_id":
                out[key] = self.guid(value) if _GUID.fullmatch(value) else self.text(value)
            else:
                out[key] = self.text(value)
        return out

    def body(self, content: bytes, content_type: str, *, is_token_request: bool) -> Any:
        if not content:
            return None
        ctype = (content_type or "").lower()
        if is_token_request or "x-www-form-urlencoded" in ctype:
            form = dict(parse_qsl(content.decode(errors="replace"), keep_blank_values=True))
            return self.token_form(form) if is_token_request else self.value(form)
        if "json" in ctype:
            import json

            try:
                return self.value(json.loads(content))
            except ValueError:
                return _redacted(content.decode(errors="replace"))
        # A MIME upload (base64 text/plain) or anything else: its size only.
        return {"redacted_body": True, "content_type": ctype.split(";")[0], "length": len(content)}

    def exchange(
        self,
        *,
        method: str,
        url: str,
        request_headers: Mapping[str, str],
        request_body: bytes,
        status: int,
        response_headers: Mapping[str, str],
        response_body: bytes,
    ) -> dict[str, Any]:
        """One request/response pair, scrubbed, in the capture file format."""
        is_token = "/oauth2/" in urlsplit(url).path
        return {
            "schema": SCHEMA,
            "request": {
                "method": method.upper(),
                "url": self.url(url),
                "headers": self.headers(request_headers, response=False),
                "body": self.body(
                    request_body,
                    request_headers.get("content-type", ""),
                    is_token_request=is_token,
                ),
            },
            "response": {
                "status": status,
                "headers": self.headers(response_headers, response=True),
                "body": self.body(
                    response_body,
                    response_headers.get("content-type", ""),
                    is_token_request=False,
                ),
            },
        }

    def rescrub(self, record: dict[str, Any]) -> dict[str, Any]:
        """Scrub an already-written capture record again (the promotion pass)."""
        out = dict(record)
        request = dict(record.get("request") or {})
        response = dict(record.get("response") or {})
        request["url"] = self.url(str(request.get("url") or ""))
        request["headers"] = self.headers(request.get("headers") or {}, response=False)
        if "/oauth2/" in str(request.get("url") or ""):
            body = request.get("body")
            if isinstance(body, dict):
                request["body"] = {
                    k: (v if k == "client_assertion_header" else self.value(v, k))
                    for k, v in body.items()
                }
        else:
            request["body"] = self.value(request.get("body"))
        response["headers"] = self.headers(response.get("headers") or {}, response=True)
        response["body"] = self.value(response.get("body"))
        out["request"], out["response"] = request, response
        return out


def _assertion_header_shape(assertion: str) -> dict[str, Any]:
    """The client assertion's JOSE header: its algorithm and WHICH thumbprints, no values."""
    import json

    head = assertion.split(".", 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except ValueError:
        return {"readable": False}
    if not isinstance(header, dict):
        return {"readable": False}
    return {
        "alg": str(header.get("alg") or ""),
        "typ": str(header.get("typ") or ""),
        "thumbprints": sorted(k for k in header if k.startswith("x5t")),
    }


# ── the promotion gate ──────────────────────────────────────────────────


def find_leaks(obj: Any, *, forbidden: Iterable[str] = (), where: str = "$") -> list[str]:
    """Where in ``obj`` real data survived. Names the location and kind, never the value.

    Flags: a JWT or bearer token, an address outside ``*.example``, a GUID the
    scrubber did not produce, an ``onmicrosoft.com`` tenant name, an IP outside
    the documentation ranges, and any ``forbidden`` literal (case-insensitive).
    """
    found: list[str] = []
    literals = [f.lower() for f in forbidden if f and len(f) >= 4]
    _walk(obj, where, literals, found)
    return found


def _walk(obj: Any, where: str, literals: list[str], found: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            _check_string(str(key), f"{where}.<key>", literals, found)
            _walk(value, f"{where}.{key}", literals, found)
        return
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            _walk(value, f"{where}[{index}]", literals, found)
        return
    if isinstance(obj, str):
        _check_string(obj, where, literals, found)


def _check_string(value: str, where: str, literals: list[str], found: list[str]) -> None:
    if _JWT.search(value):
        found.append(f"{where}: a JWT")
    if re.search(r"(?i)bearer\s+(?!<redacted>)\S", value):
        found.append(f"{where}: a bearer token")
    for match in _EMAIL.finditer(value):
        domain = match.group("domain").lower()
        if not (domain.endswith(".example") or domain == "example.com"):
            found.append(f"{where}: an address outside *.example")
    found.extend(
        f"{where}: an unscrubbed GUID"
        for match in _GUID.finditer(value)
        if not _is_scrubbed_guid(match.group(0))
    )
    if "onmicrosoft.com" in value.lower():
        found.append(f"{where}: an onmicrosoft.com tenant name")
    for match in _IPV4.finditer(value):
        try:
            ip = ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        if not (ip in ipaddress.ip_network("192.0.2.0/24") or ip.is_loopback):
            found.append(f"{where}: an IP address outside 192.0.2.0/24")
    lower = value.lower()
    found.extend(f"{where}: a forbidden literal" for literal in literals if literal in lower)
