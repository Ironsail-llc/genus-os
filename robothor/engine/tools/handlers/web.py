"""Web tool handlers — web_fetch, web_search."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote_plus, urlparse, urlsplit

import httpcore
import httpx

from robothor import __version__
from robothor.engine.tools.dispatch import ToolContext, _cfg

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# Private/loopback networks that agents must never access
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),  # "this host" — 0.0.0.0 routes to localhost
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::/128"),  # the IPv6 spelling of 0.0.0.0
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT shared space — carrier-internal
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local, incl. the v6 metadata range
    ipaddress.ip_network("fc00::/7"),
]


def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP literal falls in a blocked (private/loopback/link-local) range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before range-checking, so a
    # mapped loopback/private address can't slip past the block list.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _resolve_and_vet(url: str) -> tuple[bool, str | None]:
    """Resolve a URL's host, vet every address, and pin one to connect to.

    Returns ``(blocked, pinned_ip)``. When ``blocked`` is True the host targets a
    private/loopback/link-local address (or is unresolvable) and must not be
    fetched; ``pinned_ip`` is None. When allowed, ``pinned_ip`` is the exact
    vetted address the caller must connect to, so the request uses the same IP we
    validated — closing the DNS-rebinding TOCTOU where the name re-resolves to a
    private target between the check and the fetch. Fails CLOSED on any error.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        if hostname in ("localhost", "localhost.localdomain", ""):
            return True, None
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            return True, None

        # IP literal — no DNS; connect to the literal itself.
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if _ip_is_blocked(hostname):
                return True, None
            return False, hostname

        # Hostname — resolve every A/AAAA record and block if ANY is private.
        try:
            infos = socket.getaddrinfo(hostname, None)
        except OSError:
            return True, None  # unresolvable → fail closed
        pinned: str | None = None
        for info in infos:
            ip = str(info[4][0])
            if _ip_is_blocked(ip):
                return True, None
            if pinned is None:
                pinned = ip
        if pinned is None:
            return True, None
        return False, pinned
    except Exception:
        return True, None  # fail closed on any parsing error


def _is_blocked_host(url: str) -> bool:
    """True if a URL targets a blocked (private/loopback/link-local) host.

    Thin wrapper over :func:`_resolve_and_vet` for callers that only need the
    block decision (not the pinned address).
    """
    blocked, _ = _resolve_and_vet(url)
    return blocked


class _PinnedResolutionBackend(httpcore.AsyncNetworkBackend):
    """Dial only pre-vetted hosts, and dial them at their vetted IP.

    The pin lives at the SOCKET, not in the URL. Rewriting the request URL to
    the vetted IP literal used to be how this was done, and it broke the web:
    the origin saw a request addressed to a bare IP, so CDN-fronted sites
    answered 403, bounced through redirects the cookie jar could no longer
    follow, and certificates for hosts that list no IP SAN failed to verify.
    Here the request keeps its hostname — Host header, TLS SNI and certificate
    verification all see the real name — while ``connect_tcp`` substitutes the
    address the SSRF guard vetted.

    A host with no pin is REFUSED rather than resolved: the guard's whole point
    is that no name is looked up between the check and the connect, which is
    where DNS rebinding lives.
    """

    def __init__(self, delegate: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._pins: dict[tuple[str, int], str] = {}
        self._delegate = delegate if delegate is not None else httpcore.AnyIOBackend()
        #: Set once a connection has actually been made through this backend.
        #: web_fetch refuses a response that never set it — a transport that
        #: answered without dialling here is a transport that bypassed the pin.
        self.dialed = False

    def pin(self, host: str, port: int, ip: str) -> None:
        # Keyed by host AND port: two ports on one name are two targets, and a
        # pin vetted for :443 must not authorise a connection to :8080.
        self._pins[(host.lower(), port)] = ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        ip = self._pins.get((str(host).lower(), port))
        if ip is None:
            raise httpcore.ConnectError(
                f"refusing to connect to unvetted host {host!r} port {port}"
            )
        self.dialed = True
        return await self._delegate.connect_tcp(
            ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("refusing to connect to unvetted host (unix socket)")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose connection pool dials through a pinned backend.

    httpx exposes no constructor argument for the network backend, so the pool
    it builds is re-pointed at ours. If a future httpx stops exposing it, this
    raises instead of quietly falling back to name resolution — losing the pin
    silently would reopen the DNS-rebinding hole the guard exists to close.
    """

    def __init__(self, backend: _PinnedResolutionBackend) -> None:
        super().__init__()
        pool = self._pool
        if not hasattr(pool, "_network_backend"):  # pragma: no cover — upgrade guard
            raise RuntimeError(
                "httpx connection pool exposes no network backend to pin the vetted IP onto"
            )
        pool._network_backend = backend


def _bare_host(host: str) -> str:
    return host[4:] if host.lower().startswith("www.") else host.lower()


def _is_canonical_redirect(src: str, dst: str) -> bool:
    """True for the web's standard canonical bounce: http→https and apex↔www.

    Sites chain those two, sometimes with a trailing-slash fix, before serving
    anything. Counting them as real hops is how a perfectly ordinary site ended
    up reported as "Too many redirects".
    """
    try:
        s, d = httpx.URL(src), httpx.URL(dst)
    except Exception:
        return False
    if _bare_host(s.host) != _bare_host(d.host):
        return False
    if s.scheme != d.scheme and not (s.scheme == "http" and d.scheme == "https"):
        return False
    return s.path.rstrip("/") == d.path.rstrip("/") and s.query == d.query


def _certificate_failure(exc: BaseException) -> str | None:
    """Return the certificate-verification detail behind ``exc``, if that's what it is.

    httpx wraps the ssl error, so the reason is one or two ``__cause__`` links
    down. Verification is never disabled — a self-signed site stays a failure —
    but the agent is told the certificate is untrusted instead of getting a
    generic "Fetch failed".
    """
    err: BaseException | None = exc
    for _ in range(10):
        if err is None:
            break
        if isinstance(err, ssl.SSLCertVerificationError):
            return str(err)
        if isinstance(err, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(err):
            return str(err)
        err = err.__cause__ or err.__context__
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return str(exc)
    return None


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


#: Redirect hops that actually move the agent somewhere new.
MAX_REAL_HOPS = 5
#: Absolute ceiling including the canonical bounces that don't count as real
#: hops — a site must not be able to string those together forever.
MAX_TOTAL_HOPS = 10
#: How often one URL may reappear in a redirect chain before we call it a loop.
#: Two visits are legitimate: cookie walls bounce you out and back once.
MAX_URL_VISITS = 2

#: Default identity for outbound fetches. httpx's own default
#: (``python-httpx/<version>``) is 403'd outright by sites that block library
#: defaults — measured 2026-09-11: en.wikipedia.org answers 403 to it and 200
#: to a UA that names its client. Instances that want their own contact address
#: in it set ``ROBOTHOR_WEB_FETCH_USER_AGENT``.
USER_AGENT = "GenusOS-web-fetch/1.0 (+https://github.com/Ironsail-llc/genus-os)"


def _user_agent() -> str:
    return os.environ.get("ROBOTHOR_WEB_FETCH_USER_AGENT") or USER_AGENT


@_handler("web_fetch")
async def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = args.get("url", "")
    if not url:
        return {"error": "No URL provided"}
    try:
        import html2text
    except ImportError:
        return {"error": "html2text not installed"}

    current = url
    backend = _PinnedResolutionBackend()
    try:
        transport = _PinnedTransport(backend)
    except RuntimeError as e:  # pragma: no cover — httpx upgrade guard
        return {"error": f"Blocked: agents cannot access unvetted addresses ({e})"}

    try:
        # Redirects are followed by hand so every hop is vetted: a public host
        # must not be able to bounce the agent onto a private/loopback target.
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": _user_agent()},
        ) as client:
            visits: dict[str, int] = {current: 1}
            real_hops = 0
            total_hops = 0
            while True:
                blocked, pinned_ip = await asyncio.to_thread(_resolve_and_vet, current)
                if blocked:
                    return {
                        "error": f"Blocked: agents cannot access private/loopback addresses ({current})"
                    }
                if pinned_ip:
                    # The socket goes to the vetted address; the request keeps
                    # the hostname, so Host, SNI and cert verification match.
                    # Both spellings are pinned: the guard vets the unicode host
                    # and the transport connects under the IDNA/punycode one.
                    parsed_url = httpx.URL(current)
                    port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
                    backend.pin(parsed_url.host, port, pinned_ip)
                    backend.pin(parsed_url.raw_host.decode("ascii"), port, pinned_ip)
                resp = await client.get(current)
                if not backend.dialed:
                    # A response arrived without a connection through the pin.
                    # Every check "passed" and the guarantee is still gone, so
                    # this fails closed rather than trusting an unaccountable
                    # socket — an httpx that keeps `_network_backend` but stops
                    # routing through it must not silently restore name
                    # resolution.
                    return {
                        "error": (
                            "Blocked: agents cannot access hosts the vetted-IP pin never "
                            f"dialled — the transport bypassed the pin ({current})"
                        )
                    }

                if resp.is_redirect and not resp.headers.get("location"):
                    return {"error": f"Redirect without a Location header ({current})"}
                location = resp.headers.get("location") if resp.is_redirect else None
                if not location:
                    break
                nxt = str(httpx.URL(current).join(location))
                visits[nxt] = visits.get(nxt, 0) + 1
                if visits[nxt] > MAX_URL_VISITS:
                    return {"error": f"Redirect loop: {nxt} was returned to {visits[nxt]}×"}
                total_hops += 1
                if not _is_canonical_redirect(current, nxt):
                    real_hops += 1
                if real_hops > MAX_REAL_HOPS or total_hops > MAX_TOTAL_HOPS:
                    return {"error": f"Too many redirects (stopped at {nxt})"}
                current = nxt

            resp.raise_for_status()
            import re as _re

            cleaned = _re.sub(r"<!--.*?-->", "", resp.text, flags=_re.DOTALL)
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.body_width = 0
            text = h.handle(cleaned)
            return {"content": text[:8000], "url": current, "status": resp.status_code}
    except Exception as e:
        cert_detail = _certificate_failure(e)
        if cert_detail:
            return {
                "error": (
                    f"TLS certificate for {current} is untrusted "
                    f"(verification failed, certificate not accepted): {cert_detail}"
                )
            }
        return {"error": f"Fetch failed: {e}"}


# ---------------------------------------------------------------------------
# web_search — providers, quality gate, and the browser fallback
# ---------------------------------------------------------------------------
#
# A metasearch answer can "succeed" and still be worthless: measured on the
# production box (2026-09-11) every general SearXNG engine but bing answered
# this egress IP with "access denied"/"CAPTCHA"/"too many requests", and the
# one survivor returned generic definition pages for a local query. The
# engine's own browser tool loads the very same results page fine — so the
# search grades its answer and, when the answer is useless, drives the browser
# and says so in the result (``fallback_from``), rather than handing the agent
# plausible nothing.

_USER_AGENT = f"GenusOS/{__version__} (+https://github.com/Ironsail-llc/genus-os)"

BING_SEARCH_URL = "https://www.bing.com/search"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# A browser page is expensive; take everything organic it gives us, capped.
_MAX_BROWSER_RESULTS = 10
# Below this many on-topic rows a SearXNG answer is treated as no answer.
_MIN_USEFUL_RESULTS = 3
_MAX_PLACES = 5

# Nominatim's usage policy: at most one request per second, identifying UA.
_NOMINATIM_MIN_INTERVAL = 1.0
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0

# Words that carry no topical signal, so they never count as "the result is
# about what I asked". Only tokens longer than three characters are considered
# at all, so short glue words need no entry here.
_STOPWORDS = frozenset(
    {
        "about",
        "also",
        "another",
        "been",
        "being",
        "between",
        "both",
        "could",
        "does",
        "each",
        "else",
        "from",
        "have",
        "here",
        "into",
        "just",
        "like",
        "made",
        "make",
        "more",
        "most",
        "must",
        "near",
        "nearby",
        "only",
        "other",
        "over",
        "same",
        "should",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "using",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "will",
        "with",
        "would",
        "your",
    }
)

# Organic-result extraction, run inside the page. Kept to one expression each
# so the browser tool's `evaluate` action can take it verbatim. The Python
# parsers below mirror these selectors for the pages we capture as HTML.
_BING_EXTRACT_JS = """() => Array.from(document.querySelectorAll('li.b_algo'))
  .slice(0, 10)
  .map(li => {
    const a = li.querySelector('h2 a');
    const p = li.querySelector('.b_caption p') || li.querySelector('p');
    if (!a || !a.href) return null;
    return {
      title: (a.innerText || '').trim(),
      url: a.href,
      snippet: p ? (p.innerText || '').trim() : ''
    };
  })
  .filter(Boolean)"""

_DDG_EXTRACT_JS = """() => Array.from(document.querySelectorAll('.result'))
  .filter(el => !el.className.includes('result--ad'))
  .slice(0, 10)
  .map(el => {
    const a = el.querySelector('.result__a');
    const s = el.querySelector('.result__snippet');
    if (!a || !a.href) return null;
    let href = a.href;
    try {
      const u = new URL(href, location.href);
      const wrapped = u.searchParams.get('uddg');
      if (wrapped) href = wrapped;
    } catch (e) { /* keep href as-is */ }
    return {
      title: (a.innerText || '').trim(),
      url: href,
      snippet: s ? (s.innerText || '').trim() : ''
    };
  })
  .filter(Boolean)"""


def _row(title: str, url: str, snippet: str) -> dict[str, str]:
    """One search result in the shape every provider returns.

    ``content`` is the key the SearXNG path has always used and existing
    callers read; ``snippet`` is the name the browser/API providers use. Both
    carry the same text so neither an old caller nor a model reading the new
    providers' docs can miss it.
    """
    text = " ".join((snippet or "").split())
    return {
        "title": " ".join((title or "").split()),
        "url": url or "",
        "content": text,
        "snippet": text,
    }


def _distinctive_terms(query: str) -> set[str]:
    """The query's topical words — what a useful result should mention."""
    return {
        t for t in re.findall(r"[a-z0-9']+", query.lower()) if len(t) > 3 and t not in _STOPWORDS
    }


def _useful_count(rows: list[dict[str, str]], terms: set[str]) -> int:
    """How many rows mention at least one distinctive term of the query."""
    if not terms:
        return len(rows)
    hits = 0
    for r in rows:
        haystack = f"{r.get('title', '')} {r.get('snippet') or r.get('content', '')}".lower()
        if any(t in haystack for t in terms):
            hits += 1
    return hits


def _looks_local(query: str) -> bool:
    """True when the query is asking about somewhere, not something."""
    lowered = query.lower()
    if re.search(r"\bnear\b|\bnearby\b|\bnear\s+me\b", lowered):
        return True
    if re.search(r"\b\d{5}(?:-\d{4})?\b", query):
        return True
    # "... in Springfield" — a capitalised place after a bare "in".
    return bool(re.search(r"\bin\s+[A-Z][\w'-]+", query))


# ── HTML parsing (fallback for a page we already hold as text) ─────────

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _StackParser(HTMLParser):
    """HTMLParser with an element stack, so selectors can be ancestor-aware."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []

    # -- subclass hooks
    def on_open(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None: ...

    def on_close(self, tag: str, classes: set[str]) -> None: ...

    # -- HTMLParser plumbing
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapped = {k: (v or "") for k, v in attrs}
        classes = set(mapped.get("class", "").split())
        self.on_open(tag, mapped, classes)
        if tag not in _VOID_TAGS:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapped = {k: (v or "") for k, v in attrs}
        self.on_open(tag, mapped, set(mapped.get("class", "").split()))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                closed = self.stack[i:]
                del self.stack[i:]
                for closed_tag, closed_classes in reversed(closed):
                    self.on_close(closed_tag, closed_classes)
                return

    def in_tag(self, tag: str) -> bool:
        return any(t == tag for t, _ in self.stack)

    def in_class(self, cls: str) -> bool:
        return any(cls in classes for _, classes in self.stack)


class _BingParser(_StackParser):
    """Mirrors ``li.b_algo h2 a`` + ``.b_caption p`` on captured HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._cur: dict[str, Any] | None = None
        self._mode: str | None = None

    def on_open(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "li" and "b_algo" in classes:
            self._cur = {"title": [], "url": "", "caption": [], "para": []}
            self._mode = None
            return
        if self._cur is None:
            return
        if tag == "a" and self.in_tag("h2") and not self._cur["url"]:
            href = attrs.get("href", "")
            if href:
                self._cur["url"] = href
                self._mode = "title"
        elif tag == "p":
            self._mode = "caption" if self.in_class("b_caption") else "para"

    def on_close(self, tag: str, classes: set[str]) -> None:
        if self._cur is None:
            return
        if tag == "li" and "b_algo" in classes:
            row, self._cur, self._mode = self._cur, None, None
            url = str(row["url"])
            if url.startswith("http"):
                snippet = " ".join(row["caption"]) or " ".join(row["para"])
                self.rows.append(_row(" ".join(row["title"]), url, snippet))
            return
        if tag in ("a", "p"):
            self._mode = None

    def handle_data(self, data: str) -> None:
        if self._cur is None or self._mode is None:
            return
        text = data.strip()
        if text:
            self._cur[self._mode].append(text)


class _DdgParser(_StackParser):
    """Mirrors ``.result__a`` / ``.result__snippet`` on captured HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._cur: dict[str, Any] | None = None
        self._mode: str | None = None
        self._mode_tag: str | None = None
        self._skip = False

    def _flush(self) -> None:
        row, self._cur, self._mode = self._cur, None, None
        if row and str(row["url"]).startswith("http"):
            self.rows.append(
                _row(" ".join(row["title"]), str(row["url"]), " ".join(row["snippet"]))
            )

    def on_open(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if "result--ad" in classes or "result--sponsored" in classes:
            self._flush()
            self._skip = True
            return
        if self._skip:
            return
        if "result__a" in classes:
            self._flush()
            self._cur = {"title": [], "url": _unwrap_ddg_href(attrs.get("href", "")), "snippet": []}
            self._mode, self._mode_tag = "title", tag
        elif "result__snippet" in classes and self._cur is not None:
            self._mode, self._mode_tag = "snippet", tag

    def on_close(self, tag: str, classes: set[str]) -> None:
        if "result--ad" in classes or "result--sponsored" in classes:
            self._skip = False
            return
        if tag == self._mode_tag:
            self._mode, self._mode_tag = None, None

    def handle_data(self, data: str) -> None:
        if self._skip or self._cur is None or self._mode is None:
            return
        text = data.strip()
        if text:
            self._cur[self._mode].append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def _unwrap_ddg_href(href: str) -> str:
    """DuckDuckGo's HTML endpoint wraps results in /l/?uddg=<encoded>."""
    if not href:
        return ""
    if "uddg=" in href:
        wrapped = parse_qs(urlsplit(href).query).get("uddg", [""])[0]
        if wrapped:
            return wrapped
    if href.startswith("//"):
        return f"https:{href}"
    return href


def _parse_bing_html(html: str) -> list[dict[str, str]]:
    parser = _BingParser()
    parser.feed(html)
    parser.close()
    return parser.rows[:_MAX_BROWSER_RESULTS]


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    parser = _DdgParser()
    parser.feed(html)
    parser.close()
    return parser.rows[:_MAX_BROWSER_RESULTS]


# ── Providers ──────────────────────────────────────────────────────────


def _normalise_rows(raw: Any) -> list[dict[str, str]]:
    """Coerce whatever the page handed back into result rows."""
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith("http"):
            continue
        rows.append(
            _row(
                str(item.get("title") or ""),
                url,
                str(item.get("snippet") or item.get("content") or ""),
            )
        )
    return rows


_OUTER_HTML_JS = "() => document.documentElement.outerHTML"


async def _browser_page_results(
    browser_tool: Any,
    ctx: ToolContext,
    url: str,
    js: str,
    parse_html: Callable[[str], list[dict[str, str]]],
) -> list[dict[str, str]]:
    """Load one results page in the managed browser and extract its rows.

    In-page extraction first; if it comes back empty (CSP, a script error, a
    selector that moved) the page's own HTML is pulled out and parsed here with
    the same selectors, so an extraction failure cannot masquerade as a page
    with no results on it.
    """
    nav = await browser_tool({"action": "navigate", "url": url}, ctx)
    if nav.get("error"):
        logger.warning("web_search browser navigation failed (%s): %s", url, nav["error"])
        return []
    out = await browser_tool({"action": "evaluate", "js": js}, ctx)
    if out.get("error"):
        logger.warning("web_search browser extraction failed (%s): %s", url, out["error"])
    else:
        rows = _normalise_rows(out.get("result"))
        if rows:
            return rows

    raw = await browser_tool({"action": "evaluate", "js": _OUTER_HTML_JS}, ctx)
    html = raw.get("result")
    if isinstance(html, str) and html:
        return parse_html(html)
    return []


async def _browser_search(query: str, ctx: ToolContext) -> dict[str, Any]:
    """Search by driving the engine's own browser tool.

    Reuses the agent's browser session when one is already open — and then
    leaves it open. A session this search started is always stopped again.
    """
    from robothor.engine.tools.handlers.browser import _browser as browser_tool

    try:
        status = await browser_tool({"action": "status"}, ctx)
        started_here = status.get("status") != "running"
        if started_here:
            start = await browser_tool({"action": "start"}, ctx)
            if start.get("error"):
                return {"error": f"Browser search could not start a browser: {start['error']}"}
        try:
            sources: list[str] = []
            rows = await _browser_page_results(
                browser_tool,
                ctx,
                f"{BING_SEARCH_URL}?q={quote_plus(query)}",
                _BING_EXTRACT_JS,
                _parse_bing_html,
            )
            if rows:
                sources.append("bing")
            if len(rows) < _MIN_USEFUL_RESULTS:
                extra = await _browser_page_results(
                    browser_tool,
                    ctx,
                    f"{DDG_HTML_URL}?q={quote_plus(query)}",
                    _DDG_EXTRACT_JS,
                    _parse_ddg_html,
                )
                if extra:
                    sources.append("duckduckgo")
                    seen = {r["url"] for r in rows}
                    rows.extend(r for r in extra if r["url"] not in seen)
        finally:
            if started_here:
                await browser_tool({"action": "stop"}, ctx)
    except Exception as e:
        return {"error": f"Browser search failed: {e}"}

    capped = rows[:_MAX_BROWSER_RESULTS]
    return {
        "results": capped,
        "count": len(capped),
        "provider": "browser",
        "sources": sources,
    }


async def _brave_search(query: str, limit: int) -> list[dict[str, str]] | None:
    """Brave Search API, if the operator configured a key. None = not available."""
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                BRAVE_API_URL,
                params={"q": query, "count": max(1, min(limit, 20))},
                headers={
                    "X-Subscription-Token": key,
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Brave search failed, falling through: %s", e)
        return None
    rows = [
        _row(str(r.get("title", "")), str(r.get("url", "")), str(r.get("description", "")))
        for r in (data.get("web") or {}).get("results", [])[:limit]
        if r.get("url")
    ]
    return rows or None


async def _searxng_search(query: str, limit: int) -> dict[str, Any]:
    """Query SearXNG, reporting which engines refused to answer."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{_cfg().searxng_url}/search",
            params={"q": query, "format": "json", "pageno": 1},
        )
        resp.raise_for_status()
        data = resp.json()
    raw = data.get("results", []) or []
    rows = [
        _row(str(r.get("title", "")), str(r.get("url", "")), str(r.get("content", "")))
        for r in raw[:limit]
    ]
    names: list[str] = []
    reported: list[str] = []
    for entry in data.get("unresponsive_engines", []) or []:
        if isinstance(entry, (list, tuple)) and entry:
            names.append(str(entry[0]))
            reported.append(": ".join(str(p) for p in entry))
        elif isinstance(entry, dict):
            names.append(str(entry.get("engine", "")))
            reported.append(f"{entry.get('engine', '')}: {entry.get('error', '')}")
        else:
            names.append(str(entry))
            reported.append(str(entry))
    answering = {str(r.get("engine")) for r in raw if r.get("engine")}
    return {
        "results": rows,
        "unresponsive_engines": reported,
        # Every engine that spoke up refused — nothing actually searched.
        "all_engines_unresponsive": bool(names) and not (answering - set(names)),
    }


async def _places_lookup(query: str) -> list[dict[str, str]]:
    """Nominatim geocoding for a local-looking query, at ≤1 request/second."""
    global _nominatim_last
    try:
        async with _nominatim_lock:
            wait = _NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _nominatim_last = time.monotonic()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={"format": "jsonv2", "q": query, "limit": _MAX_PLACES},
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
    except Exception as e:
        logger.warning("Nominatim lookup failed for %r: %s", query, e)
        return []
    if not isinstance(data, list):
        return []
    places: list[dict[str, str]] = []
    for item in data[:_MAX_PLACES]:
        if not isinstance(item, dict):
            continue
        osm_type = str(item.get("osm_type", ""))
        osm_id = str(item.get("osm_id", ""))
        url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_type and osm_id else ""
        kind = " / ".join(str(p) for p in (item.get("category"), item.get("type")) if p)
        places.append(_row(str(item.get("display_name", "")), url, kind))
    return places


async def _search_with_fallback(
    query: str, limit: int, provider: str, ctx: ToolContext
) -> dict[str, Any]:
    """Pick a provider, grade what it returned, and fall back when it is empty."""
    if provider == "perplexity":
        try:
            from robothor.rag.web_search import search_perplexity

            results = await search_perplexity(query, limit=limit)
            return {"results": results, "count": len(results), "provider": "perplexity"}
        except Exception as e:
            return {"error": f"Perplexity search failed: {e}"}

    if provider == "browser":
        return await _browser_search(query, ctx)

    # An API provider, when the operator configured one, beats scraping.
    brave_rows = await _brave_search(query, limit)
    if brave_rows:
        return {"results": brave_rows, "count": len(brave_rows), "provider": "brave"}

    rows: list[dict[str, str]] = []
    unresponsive: list[str] = []
    reason = ""
    detail = ""
    try:
        searxng = await _searxng_search(query, limit)
        rows = searxng["results"]
        unresponsive = searxng["unresponsive_engines"]
        if searxng["all_engines_unresponsive"]:
            reason = "engines_unresponsive"
        elif _useful_count(rows, _distinctive_terms(query)) < min(_MIN_USEFUL_RESULTS, limit):
            reason = "low_relevance"
    except Exception as e:
        reason, detail = "error", str(e)

    if not reason:
        out: dict[str, Any] = {"results": rows, "count": len(rows), "provider": "searxng"}
        if unresponsive:
            out["unresponsive_engines"] = unresponsive
        return out

    fallback = await _browser_search(query, ctx)
    if fallback.get("error") or not fallback.get("results"):
        if rows:
            # Weak results still beat none — but say they are weak.
            return {
                "results": rows,
                "count": len(rows),
                "provider": "searxng",
                "degraded": reason,
                "unresponsive_engines": unresponsive,
            }
        return {
            "error": f"Search failed ({reason}{': ' + detail if detail else ''}); "
            f"browser fallback: {fallback.get('error') or 'no results'}",
            "unresponsive_engines": unresponsive,
        }
    fallback["fallback_from"] = "searxng"
    fallback["fallback_reason"] = reason
    fallback["unresponsive_engines"] = unresponsive
    return fallback


@_handler("web_search")
async def _web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query", "") or "")
    if not query:
        return {"error": "No query provided"}
    try:
        limit = max(1, int(args.get("limit", 5)))
    except (TypeError, ValueError):
        limit = 5
    provider = str(args.get("provider") or "searxng").strip().lower()

    result = await _search_with_fallback(query, limit, provider, ctx)

    if "error" not in result and _looks_local(query):
        places = await _places_lookup(query)
        if places:
            result["places"] = places
    return result
