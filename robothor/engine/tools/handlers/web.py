"""Web tool handlers — web_fetch, web_search."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from robothor.engine.tools.dispatch import ToolContext, _cfg

if TYPE_CHECKING:
    from collections.abc import Callable

HANDLERS: dict[str, Any] = {}

# Private/loopback networks that agents must never access
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
]


def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP literal falls in a blocked (private/loopback/link-local) range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
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


def _pin_request(
    url: str, pinned_ip: str | None
) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
    """Rewrite a request to connect to ``pinned_ip`` while preserving the original
    Host header and TLS SNI, so the socket lands on the exact vetted address.

    Returns ``(request_url, headers, extensions)``. For an IP-literal URL (host
    already equals the pinned IP) no rewrite is needed and headers/extensions are
    None.
    """
    if not pinned_ip:
        return url, None, None
    u = httpx.URL(url)
    if u.host == pinned_ip:
        return url, None, None
    host_header = u.host if u.port is None else f"{u.host}:{u.port}"
    pinned_url = str(u.copy_with(host=pinned_ip))
    return pinned_url, {"Host": host_header}, {"sni_hostname": u.host}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("web_fetch")
async def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = args.get("url", "")
    if not url:
        return {"error": "No URL provided"}
    try:
        import html2text

        # Disable auto-redirect-following and re-validate every hop, so a public
        # host cannot redirect the agent to a private/loopback target (SSRF).
        max_redirects = 5
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            current = url
            for _ in range(max_redirects + 1):
                blocked, pinned_ip = await asyncio.to_thread(_resolve_and_vet, current)
                if blocked:
                    return {
                        "error": f"Blocked: agents cannot access private/loopback addresses ({current})"
                    }
                # Pin the vetted IP onto the request so the connection cannot be
                # rerouted to a private target by a DNS swap after the check.
                req_url, req_headers, extensions = _pin_request(current, pinned_ip)
                resp = await client.get(req_url, headers=req_headers, extensions=extensions)
                if resp.is_redirect and resp.headers.get("location"):
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue
                break
            else:
                return {"error": "Too many redirects"}
            resp.raise_for_status()
            import re as _re

            cleaned = _re.sub(r"<!--.*?-->", "", resp.text, flags=_re.DOTALL)
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.body_width = 0
            text = h.handle(cleaned)
            # Report the validated hostname URL of the final hop, not the pinned-IP
            # URL we actually connected to.
            return {"content": text[:8000], "url": current, "status": resp.status_code}
    except ImportError:
        return {"error": "html2text not installed"}
    except Exception as e:
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
