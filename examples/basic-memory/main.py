"""
Basic Memory Example
====================

Demonstrates the core Robothor memory system:
  - Storing facts with vector embeddings
  - Semantic search over stored facts
  - Building and querying the entity knowledge graph

Prerequisites:
  - PostgreSQL 16 + pgvector running
  - Ollama running with qwen3-embedding:0.6b pulled
  - Database and tables created (see README.md)

Usage:
  export ROBOTHOR_DB_NAME=robothor_memory
  export ROBOTHOR_DB_USER=your_user
  python main.py
"""

import asyncio
import logging

from robothor.config import get_config
from robothor.memory.facts import extract_facts, search_facts, store_fact
from robothor.memory.entities import (
    add_relation,
    extract_and_store_entities,
    get_entity,
    upsert_entity,
)

# Set up logging so you can see what's happening
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# --- Sample content to ingest ---

SAMPLE_DOCUMENTS = [
    """
    The Horizon project launched on March 15th using FastAPI for the backend
    and React for the frontend. The team decided to use PostgreSQL with pgvector
    for semantic search capabilities. Sarah Chen leads the backend team while
    Marcus Rivera handles frontend development.
    """,
    """
    During the architecture review, the team chose Redis for caching and session
    management. The Horizon API serves approximately 10,000 requests per minute.
    Sarah Chen proposed using Kubernetes for deployment, which was approved by
    the CTO, David Park.
    """,
    """
    The machine learning pipeline for Horizon uses PyTorch for model training
    and ONNX Runtime for inference. The recommendation engine processes user
    behavior data every 15 minutes. Lisa Wang from the data science team
    built the initial prototype.
    """,
]


async def demo_store_facts() -> list[int]:
    """Extract and store facts from sample documents."""
    print("\n" + "=" * 60)
    print("STEP 1: Extracting and storing facts")
    print("=" * 60)

    all_fact_ids: list[int] = []

    for i, doc in enumerate(SAMPLE_DOCUMENTS, 1):
        print(f"\n--- Document {i} ---")
        print(f"Content preview: {doc.strip()[:80]}...")

        # Extract structured facts using the local LLM
        facts = await extract_facts(doc)
        print(f"Extracted {len(facts)} facts:")

        for fact in facts:
            print(f"  [{fact['category']}] {fact['fact_text'][:80]}...")
            print(f"    Entities: {fact['entities']}, Confidence: {fact['confidence']:.2f}")

            # Store each fact with its embedding in PostgreSQL
            fact_id = await store_fact(
                fact=fact,
                source_content=doc.strip(),
                source_type="document",
                metadata={"document_index": i, "example": True},
            )
            all_fact_ids.append(fact_id)
            print(f"    Stored as fact #{fact_id}")

    print(f"\nTotal facts stored: {len(all_fact_ids)}")
    return all_fact_ids


async def demo_search_facts():
    """Search for facts using natural language queries."""
    print("\n" + "=" * 60)
    print("STEP 2: Semantic search over stored facts")
    print("=" * 60)

    queries = [
        "What technologies does the Horizon project use?",
        "Who leads the backend development?",
        "How does the recommendation engine work?",
        "What was decided about deployment?",
    ]

    for query in queries:
        print(f"\n--- Query: \"{query}\" ---")

        results = await search_facts(query, limit=3)

        if not results:
            print("  No results found.")
            continue

        for j, result in enumerate(results, 1):
            similarity = result.get("similarity", 0)
            print(f"  {j}. [sim={similarity:.3f}] {result['fact_text'][:100]}")
            print(f"     Category: {result['category']}, Entities: {result['entities']}")


async def demo_entity_graph():
    """Build and query the entity knowledge graph."""
    print("\n" + "=" * 60)
    print("STEP 3: Building the entity knowledge graph")
    print("=" * 60)

    # Method A: Manually create entities and relations
    print("\n--- Manual entity creation ---")

    horizon_id = await upsert_entity("Horizon", "project")
    fastapi_id = await upsert_entity("FastAPI", "technology")
    react_id = await upsert_entity("React", "technology")
    sarah_id = await upsert_entity("Sarah Chen", "person")
    marcus_id = await upsert_entity("Marcus Rivera", "person")

    print(f"Created entities: Horizon(#{horizon_id}), FastAPI(#{fastapi_id}), "
          f"React(#{react_id}), Sarah(#{sarah_id}), Marcus(#{marcus_id})")

    # Add relationships between entities
    await add_relation(horizon_id, fastapi_id, "built_with")
    await add_relation(horizon_id, react_id, "built_with")
    await add_relation(sarah_id, horizon_id, "leads_backend")
    await add_relation(marcus_id, horizon_id, "leads_frontend")
    print("Added relations: Horizon->FastAPI, Horizon->React, Sarah->Horizon, Marcus->Horizon")

    # Method B: Auto-extract entities from text
    print("\n--- Automatic entity extraction from text ---")

    sample_text = (
        "Sarah Chen and Marcus Rivera presented the Horizon project "
        "to David Park. The system uses PostgreSQL, Redis, and PyTorch."
    )
    result = await extract_and_store_entities(sample_text)
    print(f"Auto-extracted: {result['entities_stored']} entities, "
          f"{result['relations_stored']} relations")


async def demo_query_graph():
    """Look up entities and their relationships."""
    print("\n" + "=" * 60)
    print("STEP 4: Querying the knowledge graph")
    print("=" * 60)

    entity_names = ["Horizon", "Sarah Chen", "FastAPI"]

    for name in entity_names:
        print(f"\n--- Entity: \"{name}\" ---")

        entity = await get_entity(name)
        if not entity:
            print(f"  Not found in knowledge graph.")
            continue

        print(f"  Type: {entity['entity_type']}")
        print(f"  Mentions: {entity['mention_count']}")
        print(f"  Last seen: {entity.get('last_seen', 'N/A')}")

        if entity.get("relations"):
            print(f"  Relations ({len(entity['relations'])}):")
            for rel in entity["relations"]:
                # Determine direction of relationship
                if rel.get("target_name"):
                    print(f"    -> {rel['relation_type']} -> {rel['target_name']} "
                          f"({rel['target_type']})")
                elif rel.get("source_name"):
                    print(f"    <- {rel['relation_type']} <- {rel['source_name']} "
                          f"({rel['source_type']})")
        else:
            print("  No relations found.")


async def main():
    """Run the full memory system demo."""
    # Show current configuration
    cfg = get_config()
    print("Robothor Memory System Demo")
    print(f"Database: {cfg.db.name} @ {cfg.db.host}:{cfg.db.port}")
    print(f"Ollama: {cfg.ollama.base_url}")
    print(f"Embedding model: {cfg.ollama.embedding_model}")

    # Step 1: Store facts
    fact_ids = await demo_store_facts()

    # Step 2: Search facts
    await demo_search_facts()

    # Step 3: Build entity graph
    await demo_entity_graph()

    # Step 4: Query the graph
    await demo_query_graph()

    print("\n" + "=" * 60)
    print("Demo complete!")
    print(f"Stored {len(fact_ids)} facts with vector embeddings.")
    print("Try modifying the SAMPLE_DOCUMENTS or queries to experiment.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
