"""Web tool handlers — web_fetch, web_search."""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from robothor.engine.tools.dispatch import ToolContext, _cfg

if TYPE_CHECKING:
    from collections.abc import Callable

HANDLERS: dict[str, Any] = {}

# Maximum redirect hops to follow manually (each hop is re-validated).
_MAX_REDIRECTS = 5

# Private/loopback networks that agents must never access
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),  # "this host" — routes to localhost on Linux
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local (cloud metadata)
    ipaddress.ip_network("fc00::/7"),  # unique-local IPv6
    ipaddress.ip_network("fe80::/10"),  # link-local IPv6
]


def _ip_is_blocked(addr: ipaddress._BaseAddress) -> bool:
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before range-checking.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _is_blocked_host(url: str) -> bool:
    """Check if a URL targets a blocked (private/loopback) host.

    Resolves hostnames to every A/AAAA record and rejects if *any* resolves
    into a blocked range — a literal-IP-only check (the prior behaviour) let
    an attacker-controlled name resolve to 127.0.0.1 / 169.254.169.254 and
    sail through (DNS-rebinding SSRF, audit 2026-05-29).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        # Block common loopback hostnames and internal-only suffixes
        if hostname in ("localhost", "localhost.localdomain", ""):
            return True
        if hostname.endswith((".local", ".internal")):
            return True

        # IP literal — check directly.
        try:
            return _ip_is_blocked(ipaddress.ip_address(hostname))
        except ValueError:
            pass

        # Hostname — resolve every address and block if ANY is private/loopback.
        try:
            infos = socket.getaddrinfo(hostname, None)
        except OSError:
            # Unresolvable — let the fetch fail normally rather than here.
            return False
        for info in infos:
            sockaddr = info[4]
            try:
                if _ip_is_blocked(ipaddress.ip_address(sockaddr[0])):
                    return True
            except ValueError:
                continue
        return False
    except Exception:
        return False


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
    if _is_blocked_host(url):
        return {"error": f"Blocked: agents cannot access private/loopback addresses ({url})"}
    try:
        import html2text

        # Manual redirect handling: a public URL can 302 to http://127.0.0.1/…,
        # so every hop is re-validated against the SSRF block (audit 2026-05-29).
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(url)
            hops = 0
            while resp.is_redirect and hops < _MAX_REDIRECTS:
                location = resp.headers.get("location", "")
                next_url = str(resp.url.join(location)) if location else ""
                if not next_url or _is_blocked_host(next_url):
                    return {
                        "error": "Blocked: redirect target is a private/loopback "
                        f"address or invalid ({next_url or location})"
                    }
                resp = await client.get(next_url)
                hops += 1
            resp.raise_for_status()
            import re as _re

            cleaned = _re.sub(r"<!--.*?-->", "", resp.text, flags=_re.DOTALL)
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.body_width = 0
            text = h.handle(cleaned)
            return {"content": text[:8000], "url": str(resp.url), "status": resp.status_code}
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
