"""Web tool handlers — web_fetch, web_search."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpcore
import httpx

from robothor.engine.tools.dispatch import ToolContext, _cfg

if TYPE_CHECKING:
    from collections.abc import Callable

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


@_handler("web_search")
async def _web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query", "")
    limit = args.get("limit", 5)
    provider = args.get("provider", "searxng")
    if not query:
        return {"error": "No query provided"}

    if provider == "perplexity":
        try:
            from robothor.rag.web_search import search_perplexity

            results = await search_perplexity(query, limit=limit)
            return {"results": results, "count": len(results), "provider": "perplexity"}
        except Exception as e:
            return {"error": f"Perplexity search failed: {e}"}

    # Fallback to SearXNG
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_cfg().searxng_url}/search",
                params={"q": query, "format": "json", "pageno": 1},
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                }
                for r in data.get("results", [])[:limit]
            ]
            return {"results": results, "count": len(results), "provider": "searxng"}
    except Exception as e:
        return {"error": f"Search failed: {e}"}
