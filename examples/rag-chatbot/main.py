"""
RAG Chatbot Example
====================

Interactive chatbot that answers questions using Retrieval-Augmented
Generation. Searches a local memory store (PostgreSQL + pgvector),
reranks results with a cross-encoder, and generates grounded answers
with source citations via a local LLM (Ollama).

Prerequisites:
  - PostgreSQL 16 + pgvector with memory tables
  - Ollama with qwen3-embedding:0.6b and qwen3-next:latest
  - Some data already ingested (run basic-memory example first)

Usage:
  python main.py           # Single-turn mode
  python main.py --chat    # Multi-turn conversation mode
"""

import argparse
import asyncio
import logging
import sys

from robothor.config import get_config
from robothor.rag.pipeline import run_pipeline
from robothor.rag.search import rag_chat

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def print_sources(result: dict) -> None:
    """Print source citations from a RAG result."""
    sources = result.get("sources", {})

    memory_sources = sources.get("memory", []) if isinstance(sources, dict) else []
    web_sources = sources.get("web", []) if isinstance(sources, dict) else []

    # Handle list-of-dicts format (from rag_query)
    if isinstance(sources, list):
        memory_sources = sources

    if memory_sources or web_sources:
        print("\n  Sources:")
        for s in memory_sources[:3]:
            tier = s.get("tier", "?")
            sim = s.get("similarity", 0)
            preview = s.get("preview", "")[:60]
            print(f"    - [{tier}, sim={sim:.3f}] {preview}...")
        for s in web_sources[:3]:
            title = s.get("title", "Untitled")
            url = s.get("url", "")
            print(f"    - [web] {title}: {url}")


def print_timing(result: dict) -> None:
    """Print timing information."""
    timing = result.get("timing", {})
    if timing:
        parts = []
        if "retrieval_ms" in timing:
            parts.append(f"retrieval={timing['retrieval_ms']}ms")
        if "search_ms" in timing:
            parts.append(f"search={timing['search_ms']}ms")
        if "rerank_ms" in timing:
            parts.append(f"rerank={timing['rerank_ms']}ms")
        if "generation_ms" in timing:
            parts.append(f"generation={timing['generation_ms']}ms")
        if "total_ms" in timing:
            parts.append(f"total={timing['total_ms']}ms")
        if parts:
            print(f"  Timing: {', '.join(parts)}")


async def single_turn_loop():
    """Run the chatbot in single-turn mode (each question is independent)."""
    print("RAG Chatbot (single-turn mode)")
    print("Type a question and press Enter. Type 'quit' to exit.\n")

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        # Run the full RAG pipeline
        print("\n  [Searching memory and generating answer...]")
        result = await run_pipeline(question)

        # Display the answer
        memories_found = result.get("memories_found", 0)
        reranked = result.get("reranked_count", 0)
        profile = result.get("profile", "unknown")

        print(f"\n  [Profile: {profile}, memories: {memories_found}, reranked: {reranked}]")
        print(f"\nAssistant: {result['answer']}")

        # Show sources and timing
        print_sources(result)
        print_timing(result)
        print()


async def multi_turn_loop():
    """Run the chatbot in multi-turn mode (maintains conversation history)."""
    print("RAG Chatbot (multi-turn mode)")
    print("Conversation history is preserved across turns.")
    print("Type a question and press Enter. Type 'quit' to exit.\n")

    messages: list[dict[str, str]] = []

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        if question.lower() == "clear":
            messages.clear()
            print("  [Conversation history cleared]\n")
            continue

        # Add user message to history
        messages.append({"role": "user", "content": question})

        # Run the multi-turn RAG chat
        print("\n  [Searching memory and generating answer...]")
        result = await rag_chat(messages)

        answer = result.get("answer", "No answer generated.")
        memories_found = result.get("memories_found", 0)

        print(f"  [Memories found: {memories_found}]")
        print(f"\nAssistant: {answer}")

        # Add assistant response to history
        messages.append({"role": "assistant", "content": answer})

        print_timing(result)
        print()


async def one_shot(question: str):
    """Answer a single question and exit (for scripting)."""
    result = await run_pipeline(question)
    print(result["answer"])

    # Print sources to stderr so stdout is clean for piping
    sources = result.get("sources", {})
    memory_sources = sources.get("memory", []) if isinstance(sources, dict) else []
    if memory_sources:
        print("\nSources:", file=sys.stderr)
        for s in memory_sources[:3]:
            print(f"  - [{s.get('tier', '?')}] {s.get('preview', '')[:60]}...", file=sys.stderr)


async def main():
    """Entry point -- parse args and run the appropriate mode."""
    parser = argparse.ArgumentParser(description="RAG Chatbot powered by Robothor")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Enable multi-turn conversation mode (preserves history)",
    )
    parser.add_argument(
        "--question",
        "-q",
        type=str,
        help="Ask a single question and exit (for scripting)",
    )
    args = parser.parse_args()

    # Show config
    cfg = get_config()
    print(f"Database: {cfg.db.name} @ {cfg.db.host}:{cfg.db.port}")
    print(f"Ollama: {cfg.ollama.base_url}")
    print(f"Generation model: {cfg.ollama.generation_model}")
    print(f"Embedding model: {cfg.ollama.embedding_model}")
    print()

    if args.question:
        await one_shot(args.question)
    elif args.chat:
        await multi_turn_loop()
    else:
        await single_turn_loop()


if __name__ == "__main__":
    asyncio.run(main())
