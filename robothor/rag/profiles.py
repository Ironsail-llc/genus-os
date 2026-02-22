"""
RAG Profiles — query classification and retrieval configuration.

Each profile controls retrieval breadth, reranking, web search,
generation temperature, and token limits.

Usage:
    from robothor.rag.profiles import classify_query, RAG_PROFILES

    profile_name = classify_query("What is quantum computing?")
    config = RAG_PROFILES[profile_name]
"""

from __future__ import annotations

RAG_PROFILES: dict[str, dict] = {
    "fast": {
        "description": "Quick answers, minimal retrieval",
        "memory_limit": 5,
        "web_limit": 3,
        "rerank_top_k": 5,
        "temperature": 0.6,
        "max_tokens": 1024,
        "use_reranker": True,
        "use_web": True,
    },
    "general": {
        "description": "Balanced retrieval and generation",
        "memory_limit": 15,
        "web_limit": 5,
        "rerank_top_k": 10,
        "temperature": 0.7,
        "max_tokens": 4096,
        "use_reranker": True,
        "use_web": True,
    },
    "research": {
        "description": "Deep retrieval, more context, thorough answers",
        "memory_limit": 30,
        "web_limit": 15,
        "rerank_top_k": 15,
        "temperature": 0.5,
        "max_tokens": 8192,
        "use_reranker": True,
        "use_web": True,
    },
    "expert": {
        "description": "Expert-level deep analysis with extensive retrieval",
        "memory_limit": 25,
        "web_limit": 50,
        "rerank_top_k": 25,
        "temperature": 0.45,
        "max_tokens": 8192,
        "use_reranker": True,
        "use_web": True,
    },
    "heavy": {
        "description": "Maximum retrieval, broadest context window",
        "memory_limit": 30,
        "web_limit": 100,
        "rerank_top_k": 30,
        "temperature": 0.5,
        "max_tokens": 8192,
        "use_reranker": True,
        "use_web": True,
    },
    "code": {
        "description": "Code-focused, precise generation",
        "memory_limit": 15,
        "web_limit": 10,
        "rerank_top_k": 10,
        "temperature": 0.6,
        "max_tokens": 4096,
        "use_reranker": True,
        "use_web": True,
    },
}

PROFILE_REQUIRED_KEYS = {
    "description", "memory_limit", "web_limit", "rerank_top_k",
    "temperature", "max_tokens", "use_reranker", "use_web",
}


CLASSIFICATION_RULES: dict[str, list[str]] = {
    "code": [
        "code", "function", "class", "def ", "import ", "error", "bug",
        "traceback", "syntax", "compile", "debug", "python", "javascript",
        "typescript", "rust", "bash", "script", "api", "endpoint",
        "database", "sql", "query", "docker", "git", "deploy",
    ],
    "research": [
        "explain in detail", "how does", "why does", "compare",
        "difference between", "pros and cons", "research", "paper",
        "study", "analysis", "deep dive", "architecture", "design",
        "theory", "concept", "history of", "explain how", "explain why",
    ],
    "expert": [
        "expert", "comprehensive", "thorough analysis", "in depth",
        "detailed breakdown", "technical deep dive", "evaluate",
        "critical analysis", "systematic review", "benchmark",
    ],
    "heavy": [
        "everything about", "all information", "exhaustive",
        "complete overview", "full report", "extensive search",
        "gather all", "maximum detail", "leave nothing out",
    ],
    "fast": [
        "what time", "weather", "quick", "simple", "yes or no",
        "one word", "briefly", "tldr", "summary", "short answer",
    ],
}


def classify_query(query: str) -> str:
    """Classify a query into a RAG profile using keyword matching.

    Args:
        query: The user's query text.

    Returns:
        Profile name: 'fast', 'code', 'research', 'expert', 'heavy', or 'general'.
    """
    query_lower = query.lower()

    scores: dict[str, int] = {profile: 0 for profile in CLASSIFICATION_RULES}
    for profile, keywords in CLASSIFICATION_RULES.items():
        for kw in keywords:
            if kw in query_lower:
                scores[profile] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] > 0:
        return best
    return "general"
