"""Web tool handlers — web_fetch, web_search."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import logging
import os
import re
import socket
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote_plus, urlparse, urlsplit

import httpcore
import httpx

from robothor import __version__
from robothor.engine.tools.dispatch import ToolContext, _audit_tool_call, _cfg

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
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# A browser page is expensive; take everything organic it gives us, capped.
_MAX_BROWSER_RESULTS = 10
# Below this many rows the browser provider reaches for a second source.
_MIN_USEFUL_RESULTS = 3
_MAX_PLACES = 5
_OVERPASS_RADIUS_M = 5000
# Whole browser fallback, including launch: an agent waiting on a search must
# not wait on a wedged Chromium.
_BROWSER_SEARCH_TIMEOUT = 45.0
# Implicit fallback only — an explicit provider="browser" always runs.
_BROWSER_FALLBACK_ENV = "ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK"

# Nominatim and Overpass are free, courtesy-use APIs: ≤1 request/second, and
# an identifying User-Agent.
_PLACES_MIN_INTERVAL = 1.0
_places_lock = asyncio.Lock()
_places_last = 0.0

# One browser fallback at a time per agent: two runs of the same agent share a
# browser session, so an unserialised pair can stop the session the other is
# still using.
_browser_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

_PROVIDERS = ("auto", "searxng", "browser", "brave", "perplexity")

# Capitalised words that follow "in" without naming a place. "how to mount a
# share in Ubuntu" is not a request for somewhere to go.
_NON_PLACE_PROPER_NOUNS = frozenset(
    {
        "android",
        "aws",
        "azure",
        "bash",
        "chrome",
        "django",
        "docker",
        "excel",
        "firefox",
        "git",
        "github",
        "gitlab",
        "go",
        "grafana",
        "java",
        "javascript",
        "kotlin",
        "kubernetes",
        "linux",
        "macos",
        "nginx",
        "node",
        "php",
        "postgres",
        "postgresql",
        "powershell",
        "python",
        "react",
        "redis",
        "ruby",
        "rust",
        "sql",
        "swift",
        "terraform",
        "typescript",
        "ubuntu",
        "vim",
        "windows",
        "word",
    }
)

# Everyday category words mapped to the OSM tags that actually find the thing.
# An unmapped category skips Overpass rather than guessing a tag.
_PLACE_CATEGORIES: dict[str, tuple[tuple[str, str], ...]] = {
    "coworking": (("office", "coworking"), ("amenity", "coworking_space")),
    "coworking space": (("office", "coworking"), ("amenity", "coworking_space")),
    "cafe": (("amenity", "cafe"),),
    "café": (("amenity", "cafe"),),
    "coffee": (("amenity", "cafe"),),
    "coffee shop": (("amenity", "cafe"),),
    "gym": (("leisure", "fitness_centre"),),
}

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
_BING_EXTRACT_JS = """() => {
  const unwrap = (href) => {
    try {
      const u = new URL(href, location.href);
      if (!u.pathname.startsWith('/ck/a')) return href;
      let p = u.searchParams.get('u') || '';
      if (p.startsWith('a1')) p = p.slice(2);
      if (!p) return href;
      let b = p.replace(/-/g, '+').replace(/_/g, '/');
      while (b.length % 4) b += '=';
      const decoded = atob(b);
      return decoded.startsWith('http') ? decoded : href;
    } catch (e) {
      return href;
    }
  };
  return Array.from(document.querySelectorAll('li.b_algo'))
    .slice(0, 10)
    .map(li => {
      const a = li.querySelector('h2 a');
      const p = li.querySelector('.b_caption p') || li.querySelector('p');
      if (!a || !a.href) return null;
      return {
        title: (a.innerText || '').trim(),
        url: unwrap(a.href),
        snippet: p ? (p.innerText || '').trim() : ''
      };
    })
    .filter(Boolean);
}"""

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


_TOKEN_RE = re.compile(r"[A-Za-z0-9'À-ɏ-]+")
# Above this share of results a term is common to the whole answer, so its
# presence says nothing about whether the answer is the one that was asked for.
_SATURATION = 0.8
# A place or number the operator named has to actually show up somewhere.
_MIN_REQUIRED_HITS = 2


def _proper_tokens(query: str) -> list[str]:
    """Capitalised words after the first — place names, brands, proper nouns."""
    tokens = _TOKEN_RE.findall(query)
    return [t for i, t in enumerate(tokens) if i > 0 and t[:1].isupper()]


def _query_terms(query: str) -> tuple[set[str], set[str]]:
    """``(distinctive, required)`` terms, both lowercased.

    Required terms — proper nouns the operator named and numbers like a ZIP —
    are the ones whose absence means the answer is about something else
    entirely. "coworking private office Jamaica Queens" must not be satisfied
    by eight pages explaining what coworking is.
    """
    distinctive: set[str] = set()
    required: set[str] = set()
    for i, token in enumerate(_TOKEN_RE.findall(query)):
        low = token.lower()
        if low in _STOPWORDS:
            continue
        is_number = bool(re.fullmatch(r"\d{4,}(?:-\d+)?", token))
        is_proper = i > 0 and token[:1].isupper()
        if is_number or (is_proper and len(low) >= 2):
            required.add(low)
            distinctive.add(low)
        elif len(low) > 3:
            distinctive.add(low)
    return distinctive, required


def _haystack(row: dict[str, str]) -> str:
    return f"{row.get('title', '')} {row.get('snippet') or row.get('content', '')}".lower()


def _grade_results(rows: list[dict[str, str]], query: str) -> str:
    """Grade an answer against the query. Empty string = good enough.

    Scores by how *rare* each term is across the results rather than by how
    many rows matched something: a term carried by nearly every row is what
    the whole result set has in common, not evidence that the result set is an
    answer. Measured failure this exists for: SearXNG returned eight "what is
    coworking" pages for "coworking private office Jamaica Queens" and a
    does-any-term-appear gate passed all eight.
    """
    if not rows:
        return "low_relevance"
    distinctive, required = _query_terms(query)
    if not distinctive:
        return ""

    frequency = {term: sum(1 for r in rows if term in _haystack(r)) for term in distinctive}
    saturation = _SATURATION * len(rows)

    missing = sorted(t for t in required if frequency.get(t, 0) < _MIN_REQUIRED_HITS)
    if missing:
        logger.info(
            "web_search: results never mention %s (query %r) — treating as no answer",
            ", ".join(missing),
            query,
        )
        return "missing_place_terms"

    matched = [t for t in distinctive if frequency[t] > 0]
    if len(matched) * 2 < len(distinctive):
        return "low_relevance"
    # Terms that discriminate between results: present, but not in nearly every
    # row. When none of them do, the only thing the results have in common is
    # the query's commonest word — fine if they matched everything asked for,
    # a topic page if some terms are missing entirely.
    discriminating = [t for t in distinctive if 0 < frequency[t] < saturation]
    if not discriminating and len(matched) < len(distinctive):
        return "low_relevance"
    return ""


def _looks_local(query: str) -> bool:
    """True when the query is asking about somewhere, not something."""
    lowered = query.lower()
    if re.search(r"\bnear\b|\bnearby\b", lowered):
        return True
    if re.search(r"\b\d{5}(?:-\d{4})?\b", query):
        return True
    # "... in Springfield" — a capitalised place after a bare "in", where the
    # capitalised word is not a piece of software.
    after_in = re.search(r"\bin\s+([A-Z][\w'-]+)", query)
    if after_in and after_in.group(1).lower() not in _NON_PLACE_PROPER_NOUNS:
        return True
    # "coworking ... Jamaica Queens" — a place-shaped category plus a proper noun.
    return bool(_place_category(query) and _proper_tokens(query))


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
            url = _unwrap_bing_href(str(row["url"]))
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


def _unwrap_bing_href(href: str) -> str:
    """Bing wraps every organic link in /ck/a?…&u=a1<base64url of the real URL>.

    Handing that to an agent hands it a click-tracker, not a source: it cannot
    be deduped, cited, or fetched. Decode it, and keep the wrapper only when
    decoding fails rather than inventing a URL.
    """
    if "/ck/a" not in href:
        return href
    try:
        encoded = parse_qs(urlsplit(href).query).get("u", [""])[0]
    except Exception:
        return href
    if not encoded:
        return href
    encoded = encoded.removeprefix("a1")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except Exception:
        return href
    return decoded if decoded.startswith("http") else href


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


# Capped: a results page is a few hundred KB, and a hostile/broken page must
# not stream megabytes of DOM back into the engine.
_OUTER_HTML_JS = "() => document.documentElement.outerHTML.slice(0, 524288)"

# Markers that say "this document really is a results page", used to tell an
# empty results page apart from an anti-bot interstitial.
_BING_RESULT_MARKERS = ("b_algo",)
_DDG_RESULT_MARKERS = ("result__a", "result__snippet")


@dataclass
class _PageOutcome:
    """What one results page gave us, and whether it was a challenge page."""

    rows: list[dict[str, str]] = field(default_factory=list)
    status: int | None = None
    blocked: bool = False


def _looks_like_challenge(html: str, markers: tuple[str, ...]) -> bool:
    """True for an anti-bot interstitial: a form, and no result markers.

    Measured: html.duckduckgo.com answers this box with HTTP 202 and a form of
    thirteen hidden inputs. Counting that as "zero results" would quietly turn
    a blocked source into an empty web.
    """
    lowered = html.lower()
    if any(m in lowered for m in markers):
        return False
    return "<form" in lowered and lowered.count("<input") >= 3


async def _browser_page_results(
    ctx: ToolContext,
    url: str,
    js: str,
    parse_html: Callable[[str], list[dict[str, str]]],
    markers: tuple[str, ...],
) -> _PageOutcome:
    """Load one results page in a throwaway tab and extract its rows.

    In-page extraction first; if it comes back empty (CSP, a script error, a
    selector that moved) the page's own HTML is parsed here with the same
    selectors, so an extraction failure cannot masquerade as a page with no
    results on it.
    """
    from robothor.engine.tools.handlers import browser as browser_mod

    out = await browser_mod.isolated_fetch(ctx, url, js, html_js=_OUTER_HTML_JS)
    if out.get("error") and not out.get("result"):
        logger.warning("web_search browser fetch failed (%s): %s", url, out["error"])
    status = out.get("status")
    final_url = str(out.get("url") or url)

    rows = _normalise_rows(out.get("result"))
    html = out.get("html")
    if not rows and isinstance(html, str) and html:
        rows = parse_html(html)

    blocked = False
    if not rows:
        challenge = isinstance(html, str) and bool(html) and _looks_like_challenge(html, markers)
        # 202 is what DuckDuckGo answers a suspected bot with; /sorry/ is
        # Google's interstitial, for whenever a Google source is added.
        blocked = status == 202 or "/sorry/" in final_url or challenge
    return _PageOutcome(
        rows=rows, status=status if isinstance(status, int) else None, blocked=blocked
    )


async def _browser_permission_error(ctx: ToolContext) -> str:
    """Empty when this caller may drive the browser, else why it may not.

    web_search reaches the browser handler directly, which means none of
    dispatch's gates are in the path. Re-run them here rather than letting a
    fallback hand browser access to a run whose toolset denies it.
    """
    from robothor.engine.permissions import check_tool_permission
    from robothor.engine.tools.dispatch import get_deferred_allowed, get_tool_whitelist

    try:
        denied = await asyncio.to_thread(
            check_tool_permission, ctx.user_role, ctx.tenant_id, "browser", user_id=ctx.user_id
        )
    except Exception as e:  # a broken RBAC lookup must not open the gate
        return f"browser permission check failed: {e}"
    if denied:
        return str(denied)
    whitelist = get_tool_whitelist()
    if whitelist is not None and "browser" not in whitelist:
        return "Tool 'browser' denied by per-task whitelist"
    deferred = get_deferred_allowed()
    if deferred is not None and "browser" not in deferred:
        return "Tool 'browser' is not in this run's allowed toolset"
    return ""


def _browser_fallback_enabled() -> bool:
    """The implicit fallback is opt-out; an explicit provider always runs.

    It drives a headed Chromium on the operator's X display, so an operator
    who does not want a search moving windows around has a switch.
    """
    return os.environ.get(_BROWSER_FALLBACK_ENV, "on").strip().lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


async def _browser_search(query: str, limit: int, ctx: ToolContext) -> dict[str, Any]:
    """Search by driving the engine's own browser in a throwaway tab.

    Reuses the agent's browser session when one is already open — and leaves it
    open, on the page the agent left it on. A session this search started is
    always stopped again.
    """
    denied = await _browser_permission_error(ctx)
    if denied:
        _audit_tool_call(
            "browser",
            ctx.agent_id,
            ctx.tenant_id,
            user_id=ctx.user_id,
            status="denied",
            error=denied,
        )
        return {"error": denied, "fallback_reason": "browser_denied"}

    cap = min(_MAX_BROWSER_RESULTS, max(limit, _MIN_USEFUL_RESULTS))
    async with _browser_locks[ctx.agent_id or "default"]:
        result = await _browser_search_locked(query, cap, ctx)
    status = "error" if result.get("error") else "ok"
    _audit_tool_call(
        "browser",
        ctx.agent_id,
        ctx.tenant_id,
        user_id=ctx.user_id,
        status=status,
        error=result.get("error"),
    )
    return result


async def _browser_search_locked(query: str, cap: int, ctx: ToolContext) -> dict[str, Any]:
    try:
        from robothor.engine.tools.handlers import browser as browser_mod

        started_here, start_error = await browser_mod.ensure_session(ctx)
        if start_error:
            return {
                "error": f"Browser search could not start a browser: {start_error}",
                "fallback_reason": "browser_unavailable",
            }
    except Exception as e:
        return {"error": f"Browser search failed: {e}", "fallback_reason": "browser_unavailable"}

    rows: list[dict[str, str]] = []
    sources: list[str] = []
    timed_out = False
    try:
        try:
            async with asyncio.timeout(_BROWSER_SEARCH_TIMEOUT):
                bing = await _browser_page_results(
                    ctx,
                    f"{BING_SEARCH_URL}?q={quote_plus(query)}",
                    _BING_EXTRACT_JS,
                    _parse_bing_html,
                    _BING_RESULT_MARKERS,
                )
                rows = list(bing.rows)
                sources.append("bing:blocked" if bing.blocked else "bing" if rows else "bing:empty")
                if len(rows) < _MIN_USEFUL_RESULTS:
                    ddg = await _browser_page_results(
                        ctx,
                        f"{DDG_HTML_URL}?q={quote_plus(query)}",
                        _DDG_EXTRACT_JS,
                        _parse_ddg_html,
                        _DDG_RESULT_MARKERS,
                    )
                    if ddg.rows:
                        sources.append("duckduckgo")
                        seen = {r["url"] for r in rows}
                        rows.extend(r for r in ddg.rows if r["url"] not in seen)
                    else:
                        sources.append("duckduckgo:blocked" if ddg.blocked else "duckduckgo:empty")
        except TimeoutError:
            timed_out = True
    except Exception as e:
        return {"error": f"Browser search failed: {e}", "fallback_reason": "browser_unavailable"}
    finally:
        # Outside the timeout scope: a cancelled scope has been un-cancelled by
        # the time we get here, so this await completes instead of re-raising.
        if started_here:
            with contextlib.suppress(Exception):
                await browser_mod.close_session(ctx)

    capped = rows[:cap]
    out: dict[str, Any] = {
        "results": capped,
        "count": len(capped),
        "provider": "browser",
        "sources": sources,
    }
    if timed_out:
        out["fallback_reason"] = "browser_timeout"
        logger.warning(
            "web_search browser fallback timed out after %.0fs (sources: %s)",
            _BROWSER_SEARCH_TIMEOUT,
            sources,
        )
    elif not capped:
        blocked_only = bool(sources) and all(s.endswith(":blocked") for s in sources)
        out["fallback_reason"] = "browser_blocked" if blocked_only else "browser_parse_empty"
        logger.warning(
            "web_search browser parsed no results (%s) — sources: %s. If the page "
            "loaded, the result selectors have moved.",
            out["fallback_reason"],
            sources,
        )
    return out


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


# ── Places: geocode, then ask OpenStreetMap for the actual category ────


def _place_category(query: str) -> str | None:
    """The everyday category word in the query, if we know its OSM tags."""
    lowered = f" {query.lower()} "
    # Longest first, so "coffee shop" beats "coffee".
    for name in sorted(_PLACE_CATEGORIES, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", lowered):
            return name
    return None


def _place_text(query: str) -> str:
    """The part of the query that names a place."""
    match = re.search(r"\b(?:near|in|around)\s+(.+)$", query, re.IGNORECASE)
    if match:
        place = match.group(1).strip(" ?.!,")
        if place.lower() in ("me", "here", "my location", "us"):
            return ""
        return place
    proper = _proper_tokens(query)
    return " ".join(proper) if proper else ""


def _overpass_query(category: str, lat: float | str, lon: float | str) -> str:
    """Overpass QL for every mapped tag of ``category`` within the radius."""
    clauses = "".join(
        f'node["{key}"="{value}"](around:{_OVERPASS_RADIUS_M},{lat},{lon});'
        f'way["{key}"="{value}"](around:{_OVERPASS_RADIUS_M},{lat},{lon});'
        for key, value in _PLACE_CATEGORIES[category]
    )
    return f"[out:json][timeout:15];({clauses});out center tags {_MAX_PLACES * 2};"


async def _courtesy_wait() -> None:
    """Hold the shared ≤1 req/s budget for the OSM APIs. Call under the lock."""
    global _places_last
    wait = _PLACES_MIN_INTERVAL - (time.monotonic() - _places_last)
    if wait > 0:
        await asyncio.sleep(wait)
    _places_last = time.monotonic()


def _short(text: str, limit: int = 40) -> str:
    """Queries are operator data; logs get a truncated echo, not the lot."""
    return text[:limit] + ("…" if len(text) > limit else "")


async def _geocode(place: str) -> tuple[str, str] | None:
    """One Nominatim lookup for a place name → ``(lat, lon)`` as given."""
    try:
        async with _places_lock:
            await _courtesy_wait()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={"format": "jsonv2", "q": place, "limit": 1},
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
    except Exception as e:
        logger.warning("Geocode failed for %r: %s", _short(place), e)
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    lat, lon = data[0].get("lat"), data[0].get("lon")
    if lat is None or lon is None:
        return None
    return str(lat), str(lon)


def _overpass_address(tags: dict[str, Any]) -> str:
    parts: list[str] = []
    street = tags.get("addr:street")
    if street:
        number = tags.get("addr:housenumber")
        parts.append(f"{number} {street}".strip() if number else str(street))
    parts.extend(
        str(tags[key])
        for key in ("addr:suburb", "addr:city", "addr:state", "addr:postcode")
        if tags.get(key)
    )
    return ", ".join(parts)


async def _overpass_places(category: str, lat: str, lon: str) -> list[dict[str, str]]:
    """Ask OpenStreetMap for places of one category near a point."""
    body = _overpass_query(category, lat, lon)
    try:
        async with _places_lock:
            await _courtesy_wait()
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    OVERPASS_URL,
                    content=body,
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
    except Exception as e:
        logger.warning("Overpass lookup failed for category %r: %s", category, e)
        return []
    elements = data.get("elements", []) if isinstance(data, dict) else []
    places: list[dict[str, str]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        osm_type, osm_id = str(element.get("type", "")), str(element.get("id", ""))
        if not osm_type or not osm_id:
            continue
        name = str(tags.get("name") or tags.get("operator") or category)
        places.append(
            _row(
                name,
                f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
                _overpass_address(tags) or category.replace("_", " "),
            )
        )
        if len(places) >= _MAX_PLACES:
            break
    return places


async def _nominatim_places(query: str) -> list[dict[str, str]]:
    """Free-text Nominatim — the fallback when no category maps to OSM tags."""
    try:
        async with _places_lock:
            await _courtesy_wait()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={"format": "jsonv2", "q": query, "limit": _MAX_PLACES},
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
    except Exception as e:
        logger.warning("Nominatim lookup failed for %r: %s", _short(query), e)
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


async def _places_lookup(query: str) -> list[dict[str, str]]:
    """Places for a local-looking query.

    Nominatim's free-text search answers "where is X", not "what X is near
    here" — asking it for "coworking near Springfield" returns whatever it can
    string-match. So for a category we have OSM tags for, geocode the place
    once and then ask Overpass for the actual amenities around that point.
    """
    category = _place_category(query)
    place = _place_text(query)
    if category and place:
        coords = await _geocode(place)
        if coords:
            rows = await _overpass_places(category, *coords)
            if rows:
                return rows
    return await _nominatim_places(query)


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
        return await _browser_search(query, limit, ctx)

    # An API provider, when the operator configured one, beats scraping — but
    # only when the caller left the choice open or asked for it. An explicit
    # provider="searxng" means SearXNG, not "whatever we think is best".
    if provider in ("auto", "brave"):
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
        else:
            reason = _grade_results(rows, query)
    except Exception as e:
        reason, detail = "error", str(e)

    if not reason:
        out: dict[str, Any] = {"results": rows, "count": len(rows), "provider": "searxng"}
        if unresponsive:
            out["unresponsive_engines"] = unresponsive
        return out

    if not _browser_fallback_enabled():
        return _degraded(rows, reason, "browser_fallback_disabled", unresponsive, detail)

    fallback = await _browser_search(query, limit, ctx)
    if fallback.get("error") or not fallback.get("results"):
        degraded = _degraded(
            rows,
            reason,
            str(fallback.get("fallback_reason") or "browser_parse_empty"),
            unresponsive,
            detail,
            browser_error=str(fallback.get("error") or ""),
        )
        if fallback.get("sources"):
            degraded["sources"] = fallback["sources"]
        return degraded

    fallback["fallback_from"] = "searxng"
    fallback["fallback_reason"] = reason
    # The browser answer gets graded too — otherwise "we fell back" reads as
    # "we fixed it" when both sources missed the question.
    browser_grade = _grade_results(fallback["results"], query)
    if browser_grade:
        fallback["degraded"] = reason
        fallback["fallback_reason"] = "browser_low_relevance"
    if unresponsive:
        fallback["unresponsive_engines"] = unresponsive
    return fallback


def _degraded(
    rows: list[dict[str, str]],
    searxng_reason: str,
    browser_reason: str,
    unresponsive: list[str],
    detail: str,
    browser_error: str = "",
) -> dict[str, Any]:
    """What to return when SearXNG was weak and the browser could not help."""
    out: dict[str, Any] = {"fallback_reason": browser_reason}
    if unresponsive:
        out["unresponsive_engines"] = unresponsive
    if rows:
        # Weak results still beat none — but say they are weak.
        out.update(
            {"results": rows, "count": len(rows), "provider": "searxng", "degraded": searxng_reason}
        )
        return out
    problem = f"{searxng_reason}{': ' + detail if detail else ''}"
    out["error"] = f"Search failed ({problem}); browser fallback: {browser_error or browser_reason}"
    return out


@_handler("web_search")
async def _web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query", "") or "")
    if not query:
        return {"error": "No query provided"}
    try:
        limit = max(1, int(args.get("limit", 5)))
    except (TypeError, ValueError):
        limit = 5
    # No provider named = "pick the best one"; a named one is honoured exactly.
    provider = str(args.get("provider") or "auto").strip().lower()
    if provider not in _PROVIDERS:
        return {
            "error": f"Unknown search provider {provider!r}. Available: {', '.join(_PROVIDERS)}"
        }

    result = await _search_with_fallback(query, limit, provider, ctx)

    if "error" not in result and _looks_local(query):
        places = await _places_lookup(query)
        if places:
            result["places"] = places
    return result
