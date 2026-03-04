#!/usr/bin/env python3
"""
Web Search module via SearXNG.

Provides web search capabilities integrated into the RAG pipeline.
SearXNG runs as a Docker container on port 8888.
"""

import asyncio

import httpx


def _get_searxng_url():
    try:
        from service_registry import get_service_url

        url = get_service_url("searxng")
        if url:
            return url
    except ImportError:
        pass
    return "http://localhost:8888"


SEARXNG_URL = _get_searxng_url()


async def search_web(
    query: str,
    limit: int = 10,
    categories: str = "general",
    language: str = "en",
    time_range: str | None = None,
) -> list[dict]:
    """Search the web via SearXNG.

    Args:
        query: Search query string.
        limit: Maximum number of results to return.
        categories: SearXNG categories (general, science, it, etc.).
        language: Language code.
        time_range: Optional time filter (day, week, month, year).

    Returns:
        List of search result dicts with 'title', 'url', 'content', 'source'.
    """
    params = {
        "q": query,
        "format": "json",
        "categories": categories,
        "language": language,
    }
    if time_range:
        params["time_range"] = time_range

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{SEARXNG_URL}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return []  # SearXNG not running
    except Exception:
        return []

    results = []
    for r in data.get("results", [])[:limit]:
        results.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "source": r.get("engine", "web"),
                "score": r.get("score", 0.0),
            }
        )

    return results


async def check_searxng_available() -> bool:
    """Check if SearXNG is running and accessible."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{SEARXNG_URL}/healthz")
            return resp.status_code == 200
    except Exception:
        # Try the search endpoint as fallback health check
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={"q": "test", "format": "json"},
                )
                return resp.status_code == 200
        except Exception:
            return False


def format_web_results(results: list[dict], max_chars: int = 4000) -> str:
    """Format web search results into a context string for the LLM.

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


def web_results_to_memory_format(results: list[dict]) -> list[dict]:
    """Convert web results to the same format as memory search results.

    This allows web results to be merged with memory results and
    passed through the reranker.
    """
    formatted = []
    for r in results:
        formatted.append(
            {
                "content": f"{r['title']}\n{r['content']}",
                "content_type": "web_search",
                "tier": "web",
                "similarity": min(r.get("score", 0.5), 1.0),
                "metadata": {"url": r["url"], "source": r["source"]},
            }
        )
    return formatted


# Synchronous wrapper
def search_web_sync(query: str, **kwargs) -> list[dict]:
    """Synchronous wrapper for search_web()."""
    return asyncio.run(search_web(query, **kwargs))


if __name__ == "__main__":
    import sys

    async def main():
        available = await check_searxng_available()
        print(f"SearXNG available: {available}")

        if available and len(sys.argv) > 1:
            query = " ".join(sys.argv[1:])
            print(f"\nSearching: {query}")
            results = await search_web(query, limit=5)
            for r in results:
                print(f"\n  [{r['source']}] {r['title']}")
                print(f"  {r['url']}")
                print(f"  {r['content'][:150]}...")
        elif not available:
            print("Start SearXNG: docker compose -f docker-compose.searxng.yml up -d")

    asyncio.run(main())
