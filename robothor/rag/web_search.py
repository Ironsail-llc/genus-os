"""
Web search via SearXNG (local, private search engine).

Integrates web results into the RAG pipeline alongside memory search.
SearXNG runs as a Docker container — no cloud API needed.

Usage:
    from robothor.rag.web_search import search_web, web_results_to_memory_format

    results = await search_web("quantum computing advances", limit=10)
    memory_fmt = web_results_to_memory_format(results)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


def _searxng_url() -> str:
    """Get SearXNG URL from config or env."""
    url = os.environ.get("ROBOTHOR_SEARXNG_URL")
    if url:
        return url
    try:
        from robothor.services.registry import get_service_url

        svc_url = get_service_url("searxng")
        if svc_url:
            return svc_url
    except Exception:
        pass
    return "http://localhost:8888"


async def search_web(
    query: str,
    limit: int = 10,
    categories: str = "general",
    language: str = "en",
    time_range: str | None = None,
) -> list[dict[str, Any]]:
    """Search the web via SearXNG.

    Args:
        query: Search query string.
        limit: Maximum number of results to return.
        categories: SearXNG categories (general, science, it, etc.).
        language: Language code.
        time_range: Optional time filter (day, week, month, year).

    Returns:
        List of result dicts with 'title', 'url', 'content', 'source', 'score'.
    """
    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "categories": categories,
        "language": language,
    }
    if time_range:
        params["time_range"] = time_range

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_searxng_url()}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return []
    except Exception:
        return []

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "source": r.get("engine", "web"),
            "score": r.get("score", 0.0),
        }
        for r in data.get("results", [])[:limit]
    ]

    return results


async def search_perplexity(
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search using Perplexity API (OpenAI-compatible via litellm).

    Requires PERPLEXITY_API_KEY in environment. Uses the sonar model
    which returns grounded answers with source citations.

    Returns same format as search_web() for interchangeability.
    """
    try:
        import litellm

        response = await litellm.acompletion(
            model="perplexity/sonar-pro",
            messages=[{"role": "user", "content": query}],
            temperature=0.0,
            max_tokens=1000,
        )

        content = response.choices[0].message.content or ""

        # Perplexity returns a single answer with citations
        # Package as a search result for consistency
        results = [
            {
                "title": f"Perplexity: {query[:60]}",
                "url": "",
                "content": content,
                "source": "perplexity",
                "score": 1.0,
            }
        ]

        # Extract citations — check multiple locations (litellm may surface them differently)
        citations = (
            getattr(response, "citations", None)
            or getattr(response, "_hidden_params", {}).get("citations")
            or []
        )
        if citations and isinstance(citations, list):
            for i, url in enumerate(citations[: limit - 1]):
                results.append(
                    {
                        "title": f"Source {i + 1}",
                        "url": url if isinstance(url, str) else str(url),
                        "content": "",
                        "source": "perplexity",
                        "score": 0.8,
                    }
                )

        return results[:limit]

    except Exception as e:
        return [
            {
                "title": "Perplexity search failed",
                "url": "",
                "content": str(e),
                "source": "perplexity",
                "score": 0,
            }
        ]


async def check_searxng_available() -> bool:
    """Check if SearXNG is running and accessible."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_searxng_url()}/healthz")
            return resp.status_code == 200
    except Exception:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{_searxng_url()}/search",
                    params={"q": "test", "format": "json"},
                )
                return resp.status_code == 200
        except Exception:
            return False


def format_web_results(results: list[dict[str, Any]], max_chars: int = 4000) -> str:
    """Format web search results into a context string.

    Args:
        results: List of web search results.
        max_chars: Maximum characters for the formatted string.

    Returns:
        Formatted string of web results.
    """
    if not results:
        return "No web results found."

    parts = []
    total = 0
    for i, r in enumerate(results, 1):
        entry = f"[Web {i}] {r['title']}\nURL: {r['url']}\n{r['content']}"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    return "\n\n---\n\n".join(parts)


def web_results_to_memory_format(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert web results to the same format as memory search results.

    This allows web results to be merged with memory results and
    passed through the reranker.
    """
    formatted = [
        {
            "content": f"{r['title']}\n{r['content']}",
            "content_type": "web_search",
            "tier": "web",
            "similarity": min(r.get("score", 0.5), 1.0),
            "metadata": {"url": r["url"], "source": r["source"]},
        }
        for r in results
    ]
    return formatted


def search_web_sync(query: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Synchronous wrapper for search_web()."""
    return asyncio.run(search_web(query, **kwargs))
