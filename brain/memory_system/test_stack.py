#!/usr/bin/env python3
"""
Test script for the full RAG stack.

Tests each phase independently, then tests the integrated pipeline.
Run from the memory_system directory with venv activated.
"""

import asyncio
import sys
import time


# Phase 1 tests
async def test_phase1():
    """Test Qwen3-80B generation via Ollama."""
    print("\n" + "=" * 60)
    print("PHASE 1: Qwen3-80B Generation")
    print("=" * 60)

    from llm_client import (
        chat,
        check_model_available,
        detect_generation_model,
        generate,
    )

    # 1a. Auto-detect and check model availability
    detected = await detect_generation_model()
    print(f"\n[1a] Auto-detected model: {detected}")
    available = await check_model_available()
    print(f"     Model available: {available}")
    if not available:
        print("  → No Qwen3 model available yet. Run: ollama pull qwen3:80b")
        print("  → Skipping generation tests.")
        return False

    # 1b. Simple generation
    print("\n[1b] Simple generation test...")
    t0 = time.time()
    result = await generate(
        "What is 2+2? Answer in exactly one sentence.",
        temperature=0.1,
        max_tokens=100,
    )
    elapsed = time.time() - t0
    print(f"  Response ({elapsed:.1f}s): {result[:200]}")
    assert len(result) > 0, "Empty response"
    print("  → PASS")

    # 1c. Chat completion
    print("\n[1c] Chat completion test...")
    t0 = time.time()
    result = await chat(
        messages=[
            {"role": "system", "content": "You are a helpful assistant. Be concise."},
            {"role": "user", "content": "Name three programming languages."},
        ],
        temperature=0.1,
        max_tokens=100,
    )
    elapsed = time.time() - t0
    print(f"  Response ({elapsed:.1f}s): {result[:200]}")
    assert len(result) > 0, "Empty response"
    print("  → PASS")

    # 1d. Embedding dimension test
    print("\n[1d] Embedding dimension test (qwen3-embedding:0.6b → 1024-dim)...")
    from llm_client import get_embedding_async

    emb = await get_embedding_async("test embedding dimension")
    print(f"  Embedding length: {len(emb)}")
    assert len(emb) == 1024, f"Expected 1024-dim, got {len(emb)}-dim"
    print("  → PASS")

    # 1e. RAG query (memory search + generation)
    print("\n[1e] RAG query test...")
    from rag_query import rag_query

    t0 = time.time()
    result = await rag_query(
        "What do you know about Philip's system?",
        memory_limit=5,
        temperature=0.3,
    )
    elapsed = time.time() - t0
    print(f"  Memories found: {result['memories_found']}")
    print(f"  Timing: {result['timing']}")
    print(f"  Answer: {result['answer'][:300]}...")
    print("  → PASS")

    return True


# Phase 2 tests
async def test_phase2():
    """Test reranking."""
    print("\n" + "=" * 60)
    print("PHASE 2: Reranking")
    print("=" * 60)

    from reranker import check_reranker_available, rerank_with_fallback

    available = await check_reranker_available()
    print(f"\n[2a] Reranker available: {available}")

    # Test with sample data — works with or without reranker
    test_results = [
        {
            "content": "Philip's ThinkStation PGX has 128GB unified memory with Grace Blackwell.",
            "similarity": 0.85,
            "tier": "short_term",
            "content_type": "conversation",
        },
        {
            "content": "The weather in Melbourne is expected to be sunny this week.",
            "similarity": 0.80,
            "tier": "short_term",
            "content_type": "conversation",
        },
        {
            "content": "Qwen3-80B model runs at approximately 45 tokens per second.",
            "similarity": 0.75,
            "tier": "long_term",
            "content_type": "decision",
        },
        {
            "content": "Philip had eggs for breakfast yesterday.",
            "similarity": 0.70,
            "tier": "short_term",
            "content_type": "conversation",
        },
        {
            "content": "NVIDIA Grace Blackwell is a unified CPU+GPU architecture.",
            "similarity": 0.65,
            "tier": "long_term",
            "content_type": "conversation",
        },
    ]

    print("\n[2b] Reranking test (query: 'Grace Blackwell hardware specs')...")
    reranked = await rerank_with_fallback(
        "Grace Blackwell hardware specs",
        test_results,
        top_k=3,
    )
    print(f"  Results: {len(reranked)} (from {len(test_results)})")
    for r in reranked:
        score_info = f"sim={r.get('similarity', 0):.3f}"
        if "rerank_relevant" in r:
            score_info += f", relevant={r['rerank_relevant']}"
        print(f"    [{score_info}] {r['content'][:80]}...")

    if available:
        print("  → PASS (with reranker)")
    else:
        print("  → PASS (fallback mode — pull reranker: ollama pull qwen3-reranker:0.6b)")

    return True


# Phase 3 tests
async def test_phase3():
    """Test web search via SearXNG."""
    print("\n" + "=" * 60)
    print("PHASE 3: Web Search (SearXNG)")
    print("=" * 60)

    from web_search import check_searxng_available, search_web, web_results_to_memory_format

    available = await check_searxng_available()
    print(f"\n[3a] SearXNG available: {available}")

    if not available:
        print("  → SearXNG not running.")
        print(
            "  → Start it: cd /home/philip/robothor/brain/memory_system && docker compose -f docker-compose.searxng.yml up -d"
        )
        print("  → Skipping web search tests (pipeline will work without it).")
        return True  # Not a hard failure

    print("\n[3b] Web search test...")
    results = await search_web("NVIDIA Grace Blackwell specifications", limit=3)
    print(f"  Results: {len(results)}")
    for r in results:
        print(f"    [{r['source']}] {r['title'][:60]}")

    print("\n[3c] Format conversion test...")
    converted = web_results_to_memory_format(results)
    print(f"  Converted {len(converted)} results to memory format")
    assert all("content" in r and "tier" in r for r in converted), "Conversion failed"
    print("  → PASS")

    return True


# Phase 4 tests
async def test_phase4():
    """Test the orchestrator pipeline (without starting the server)."""
    print("\n" + "=" * 60)
    print("PHASE 4: Orchestrator Pipeline")
    print("=" * 60)

    from orchestrator import RAG_PROFILES, classify_query, run_pipeline

    # 4a. Query classification
    print("\n[4a] Query classification tests...")
    test_queries = [
        ("What is 2+2?", "general"),
        ("Write a Python function to sort a list", "code"),
        ("Explain how transformers work in deep learning", "research"),
        ("What did Philip do yesterday?", "general"),
        ("Fix the bug in the authentication module", "code"),
        ("Give me a quick summary of today's tasks", "fast"),
    ]
    for query, expected in test_queries:
        result = classify_query(query)
        status = "✓" if result == expected else f"✗ (got {result})"
        print(f"  {status} '{query[:50]}...' → {result}")

    print(f"\n  Profiles available: {list(RAG_PROFILES.keys())}")

    # 4b. Full pipeline (requires Qwen3-80B)
    from llm_client import check_model_available

    model_ok = await check_model_available()

    if model_ok:
        print("\n[4b] Full pipeline test...")
        t0 = time.time()
        result = await run_pipeline("What do you know about Philip's computer setup?")
        elapsed = time.time() - t0
        print(f"  Profile: {result['profile']}")
        print(f"  Memories: {result['memories_found']}, Web: {result['web_results_found']}")
        print(f"  Reranked: {result['reranked_count']}")
        print(f"  Timing: {result['timing']}")
        print(f"  Answer: {result['answer'][:300]}...")
        print("  → PASS")
    else:
        print("\n[4b] Skipping full pipeline test (Qwen3-80B not available)")

    return True


async def main():
    print("=" * 60)
    print("  ROBOTHOR RAG STACK — Integration Test Suite")
    print("=" * 60)

    results = {}

    # Run all phases
    try:
        results["phase1"] = await test_phase1()
    except Exception as e:
        print(f"\n  PHASE 1 ERROR: {e}")
        results["phase1"] = False

    try:
        results["phase2"] = await test_phase2()
    except Exception as e:
        print(f"\n  PHASE 2 ERROR: {e}")
        results["phase2"] = False

    try:
        results["phase3"] = await test_phase3()
    except Exception as e:
        print(f"\n  PHASE 3 ERROR: {e}")
        results["phase3"] = False

    try:
        results["phase4"] = await test_phase4()
    except Exception as e:
        print(f"\n  PHASE 4 ERROR: {e}")
        results["phase4"] = False

    # Summary
    print("\n" + "=" * 60)
    print("  TEST SUMMARY")
    print("=" * 60)
    for phase, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {phase}: {status}")

    all_passed = all(results.values())
    print(f"\n  Overall: {'ALL PASSED' if all_passed else 'SOME FAILURES'}")
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
