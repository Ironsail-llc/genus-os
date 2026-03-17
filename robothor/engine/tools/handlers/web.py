"""Web tool handlers — web_fetch, web_search."""

from __future__ import annotations

import ipaddress
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


def _is_blocked_host(url: str) -> bool:
    """Check if a URL targets a blocked (private/loopback) host."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        # Block common loopback hostnames
        if hostname in ("localhost", "localhost.localdomain", ""):
            return True

        # Resolve and check against blocked networks
        try:
            addr = ipaddress.ip_address(hostname)
            return any(addr in net for net in _BLOCKED_NETWORKS)
        except ValueError:
            # Not an IP literal — could be a hostname
            # Block common internal patterns
            return hostname.endswith(".local") or hostname.endswith(".internal")
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

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
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
