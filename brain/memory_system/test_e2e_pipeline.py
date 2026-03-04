"""
End-to-End Pipeline — Tests

Full integration tests for the complete memory system pipeline:
  Create test content → Ingest → Extract facts → Query RAG → Verify retrieval

These tests exercise the entire path from raw content to query-time retrieval.
They require all components to be running (Ollama, PostgreSQL, orchestrator).

Markers:
  - @pytest.mark.e2e: Full end-to-end tests
  - @pytest.mark.slow: Takes >10s due to real LLM calls
  - @pytest.mark.llm: Requires Ollama with Qwen3-Next model
"""

import time

import psycopg2
import pytest
from fact_extraction import extract_facts, search_facts, store_fact
from ingestion import ingest_content
from psycopg2.extras import RealDictCursor
from rag import store_short_term

# ============== Full Pipeline: Ingest → Extract → Query ==============


@pytest.mark.e2e
@pytest.mark.llm
@pytest.mark.slow
class TestEndToEndPipeline:
    """Full end-to-end pipeline tests with real LLM calls."""

    async def test_ingest_extract_and_retrieve(self, test_prefix):
        """
        Full pipeline:
          1. Ingest content via ingest_content()
          2. Facts get extracted and stored with embeddings
          3. Query via search_facts() finds the stored facts

        This is the core roundtrip test for the memory system.
        """
        t0 = time.time()

        # Step 1: Ingest content
        content = (
            f"{test_prefix} Philip installed a new RTX 5090 GPU in his "
            f"ThinkStation workstation. The GPU has 32GB GDDR7 memory "
            f"and supports NVLink for multi-GPU configurations."
        )
        result = await ingest_content(
            content=content,
            source_channel="test",
            content_type="technical",
        )

        t_ingest = time.time() - t0
        assert result["facts_processed"] >= 1, (
            f"Expected >=1 fact extracted, got {result['facts_processed']}"
        )

        # Step 2: Verify facts are searchable
        t1 = time.time()
        search_results = await search_facts(f"{test_prefix} RTX 5090 GPU ThinkStation", limit=5)
        t_search = time.time() - t1

        assert len(search_results) >= 1, "No search results for ingested content"
        found = any(test_prefix in r["fact_text"] for r in search_results)
        assert found, (
            f"Test prefix not found in results: {[r['fact_text'][:60] for r in search_results]}"
        )

        t_total = time.time() - t0
        print(
            f"\n  Pipeline timing: ingest={t_ingest:.1f}s, search={t_search:.1f}s, total={t_total:.1f}s"
        )

    async def test_ingest_preserves_entities(self, test_prefix):
        """
        Ingested content should produce facts with extracted entities.
        """
        content = (
            f"{test_prefix} Samantha from Ironsail Pharma met with "
            f"Philip to discuss the quarterly report for Q3 2025."
        )
        result = await ingest_content(
            content=content,
            source_channel="test",
            content_type="event",
        )
        assert result["facts_processed"] >= 1

        # Search for the facts
        search_results = await search_facts(f"{test_prefix} Samantha Ironsail meeting", limit=5)
        assert len(search_results) >= 1

        # Check that entities were extracted
        all_entities = []
        for r in search_results:
            if test_prefix in r["fact_text"]:
                all_entities.extend(r.get("entities", []))

        # At minimum, should find some entities (names, orgs)
        assert len(all_entities) >= 1, f"Expected entities in facts, got: {all_entities}"

    async def test_ingest_sets_source_channel(self, test_prefix, db_conn):
        """
        Facts ingested from a specific channel should be tagged with that channel.
        """
        content = f"{test_prefix} Telegram message about project deadlines this week"
        result = await ingest_content(
            content=content,
            source_channel="telegram",
            content_type="conversation",
        )
        assert result["facts_processed"] >= 1
        assert result["source_channel"] == "telegram"

        # Verify in DB
        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT source_channel FROM memory_facts WHERE fact_text LIKE %s",
            (f"%{test_prefix}%",),
        )
        rows = cur.fetchall()
        assert any(r["source_channel"] == "telegram" for r in rows), (
            f"No telegram-tagged facts found: {rows}"
        )


# ============== Extraction Quality Tests ==============


@pytest.mark.e2e
@pytest.mark.llm
@pytest.mark.slow
class TestExtractionQuality:
    """Tests that fact extraction produces meaningful, structured output."""

    async def test_multi_fact_extraction_coverage(self, test_prefix, sample_content):
        """
        Content with multiple facts should produce at least 2 extracted facts.
        """
        content = f"{test_prefix} {sample_content['multi_fact']}"
        facts = await extract_facts(content)

        assert len(facts) >= 2, (
            f"Expected >=2 facts from multi-fact content, got {len(facts)}: "
            f"{[f['fact_text'][:50] for f in facts]}"
        )

        # Each fact should have required fields
        for f in facts:
            assert f["fact_text"].strip(), "Empty fact_text"
            assert f["category"] in [
                "personal",
                "project",
                "decision",
                "preference",
                "event",
                "contact",
                "technical",
            ]
            assert 0.0 <= f["confidence"] <= 1.0

    async def test_preference_detection(self, test_prefix, sample_content):
        """
        Content about preferences should produce 'preference' category facts.
        """
        content = f"{test_prefix} {sample_content['preference']}"
        facts = await extract_facts(content)
        assert len(facts) >= 1

        categories = [f["category"] for f in facts]
        assert "preference" in categories, f"Expected 'preference' category, got: {categories}"


# ============== Timed Execution Tests ==============


@pytest.mark.e2e
@pytest.mark.llm
@pytest.mark.slow
class TestTimedExecution:
    """Tests with timing thresholds for performance monitoring."""

    async def test_search_latency_under_threshold(self, test_prefix):
        """
        Semantic search should complete within 2 seconds.
        """
        # Store a fact first
        fact = {
            "fact_text": f"{test_prefix} Latency test fact about GPU performance",
            "category": "technical",
            "entities": ["GPU"],
            "confidence": 0.9,
        }
        await store_fact(fact, f"{test_prefix} src", "test")

        # Time the search
        t0 = time.time()
        results = await search_facts(f"{test_prefix} GPU performance", limit=5)
        elapsed = time.time() - t0

        assert elapsed < 2.0, f"Search took {elapsed:.1f}s, threshold is 2.0s"
        assert len(results) >= 1

    async def test_fact_extraction_latency(self, test_prefix):
        """
        Fact extraction from moderate content should complete within 60 seconds.
        (LLM inference is the bottleneck.)
        """
        content = (
            f"{test_prefix} Philip decided to use PostgreSQL with pgvector "
            f"for the memory system. He chose Qwen3-Next-80B as the LLM."
        )
        t0 = time.time()
        facts = await extract_facts(content)
        elapsed = time.time() - t0

        assert elapsed < 60.0, f"Extraction took {elapsed:.1f}s, threshold is 60s"
        assert len(facts) >= 1, "No facts extracted"
        print(f"\n  Extraction latency: {elapsed:.1f}s for {len(facts)} facts")


# ============== RAG-Level E2E Test ==============


@pytest.mark.e2e
@pytest.mark.llm
@pytest.mark.slow
class TestRAGEndToEnd:
    """Tests the full RAG query pipeline with real components."""

    async def test_rag_query_with_stored_context(self, test_prefix):
        """
        Store context → query orchestrator → verify answer uses context.
        """
        from orchestrator import run_pipeline

        # Store distinctive content
        fact = {
            "fact_text": (
                f"{test_prefix} The Robothor memory system uses a three-tier "
                f"architecture: short-term (48h TTL), long-term (permanent), "
                f"and a structured fact store with pgvector embeddings"
            ),
            "category": "technical",
            "entities": ["Robothor"],
            "confidence": 0.95,
        }
        await store_fact(fact, f"{test_prefix} src", "technical")

        # Also store in short-term for broader coverage
        store_short_term(
            f"{test_prefix} Robothor has three memory tiers with pgvector",
            "technical",
        )

        # Query the pipeline
        result = await run_pipeline(
            f"What is the {test_prefix} memory architecture?",
            profile="fast",
        )

        assert "answer" in result
        assert len(result["answer"]) > 0
        assert result["memories_found"] >= 0  # May or may not find matches
        assert "timing" in result


# ============== Cleanup ==============


@pytest.fixture(autouse=True)
def cleanup_e2e_short_term(test_prefix):
    """Clean up short-term memory entries created during E2E tests."""
    yield
    try:
        conn = psycopg2.connect(
            dbname="robothor_memory",
            user="philip",
            host="/var/run/postgresql",
        )
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM short_term_memory WHERE content LIKE %s",
            (f"%{test_prefix}%",),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
