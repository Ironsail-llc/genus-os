# Robothor Testing Strategy

Deep reference for the testing rules in `CLAUDE.md` (rules 11-15).

## Test Classification

### Level 1: Unit Tests (no marker)

Fast (<1s), all dependencies mocked, no I/O. These are the default and make up the bulk of the suite.

**Examples:**
- `classify_query("what time is it")` returns `"fast"` — pure function, no mocking needed
- Bridge `/health` with mocked HTTP responses → returns `{"status": "ok"}`
- `contact_resolver.resolve()` with mocked DB + mocked HTTP → returns mapping dict

### Level 2: Integration Tests (`@pytest.mark.integration`)

Real database or Redis, but no external services. Use `test_prefix` fixture for data isolation.

**Examples:**
- Insert into `contact_identifiers`, call `resolve()` with real DB, verify upsert logic
- Memory fact ingestion → pgvector search → verify recall

### Level 3: AI Behavior Tests (`@pytest.mark.llm`)

Require Ollama running with the model loaded. Test output properties, not exact strings.

**Examples:**
- `intelligence_pipeline.py` dry run → output has expected phases and structure
- RAG pipeline: query → verify response mentions relevant memory facts
- Fact extraction: feed known text → verify >=3 facts extracted with correct categories

### Level 4: End-to-End Tests (`@pytest.mark.e2e`)

Full system running (all services, Docker containers, crons). Slowest, run least often.

**Examples:**
- Email arrives → email_sync.py → triage worker → Telegram notification
- Voice call → transcription → memory ingestion → fact extraction
- CRM message → Bridge webhook → contact resolution → memory storage

### Smoke Tests (`@pytest.mark.smoke`)

Infrastructure health checks. Fast but require services to be running.

**Examples:**
- `systemctl is-active robothor-bridge` → active
- `GET /health` on each service → 200 with expected structure
- Docker containers running → `docker ps` shows expected containers

---

## AI Testing Patterns

### 1. Property-Based Validation

Don't assert exact LLM output. Assert structural properties:

```python
# Good: test structure
result = await extract_facts(text)
assert isinstance(result, list)
assert len(result) >= 1
for fact in result:
    assert "fact_text" in fact
    assert "category" in fact
    assert fact["category"] in VALID_CATEGORIES
    assert 0 <= fact["confidence"] <= 1.0

# Bad: test exact output
assert result[0]["fact_text"] == "Philip uses Neovim"
```

### 2. Golden Datasets

Curated input/output pairs. Test for >=80% match rate, not 100%.

```python
GOLDEN = [
    ("Philip uses VS Code", {"category": "preference", "entities": ["Philip", "VS Code"]}),
    ...
]

@pytest.mark.llm
def test_fact_extraction_golden():
    matches = 0
    for text, expected in GOLDEN:
        result = extract_facts(text)
        if result[0]["category"] == expected["category"]:
            matches += 1
    assert matches / len(GOLDEN) >= 0.8
```

### 3. Semantic Similarity

For free-text outputs, use embedding similarity rather than exact match:

```python
@pytest.mark.llm
def test_summary_relevance():
    summary = generate_summary(input_text)
    sim = cosine_similarity(embed(summary), embed(expected_topic))
    assert sim > 0.7
```

### 4. Adversarial Edge Cases

Test that the system handles malformed or adversarial input gracefully:

```python
def test_extract_facts_empty_input():
    assert extract_facts("") == []

def test_extract_facts_gibberish():
    result = extract_facts("asdf jkl; qwerty zxcv")
    assert isinstance(result, list)  # doesn't crash
```

### 5. Mock LLMs in Unit Tests

For unit tests, mock the LLM call and return canned responses:

```python
def test_pipeline_with_mock_llm(mock_llm):
    mock_llm.return_value = '{"facts": [{"text": "test", "category": "general"}]}'
    result = run_extraction("some input")
    assert len(result) == 1
```

---

## TDD Workflow

### New Feature

1. Write a failing test that describes the expected behavior
2. Implement the minimum code to make it pass
3. Refactor while keeping tests green

### Bug Fix

1. Write a regression test that reproduces the bug (it should fail)
2. Fix the bug
3. Verify the test passes
4. The regression test stays in the suite permanently

### Refactoring

1. Write characterization tests that capture current behavior
2. Refactor the code
3. Verify characterization tests still pass

---

## Shared Fixtures Pattern

Every test module directory gets a `conftest.py`. Follow the pattern from `brain/memory_system/conftest.py`:

```python
import uuid
import pytest

@pytest.fixture
def test_prefix():
    """Unique tag for test data isolation and cleanup."""
    return f"__test_{uuid.uuid4().hex[:8]}__"

@pytest.fixture
def db_conn():
    """Real PostgreSQL connection (for integration tests)."""
    conn = psycopg2.connect(...)
    conn.autocommit = False
    yield conn
    conn.close()

@pytest.fixture(autouse=True)
def cleanup_test_data(test_prefix, db_conn):
    """Delete all rows tagged with test_prefix after each test."""
    yield
    # DELETE FROM ... WHERE ... LIKE %test_prefix%
    db_conn.commit()
```

**FastAPI fixtures** (for service tests like Bridge):

```python
import httpx
from httpx import ASGITransport

@pytest.fixture
async def test_client():
    """Async HTTP client wrapping the FastAPI app."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
```

**Mock HTTP fixtures** (for isolating external calls):

```python
from unittest.mock import AsyncMock, patch

@pytest.fixture
def mock_crm_dal():
    """Mock CRM data access layer (crm_dal) responses."""
    with patch("crm_dal.list_people") as list_p, \
         patch("crm_dal.create_person") as create_p, \
         patch("crm_dal.list_conversations") as list_c:
        yield {"list_people": list_p, "create_person": create_p, "list_conversations": list_c}
```

---

## Existing Test Suites

Tests that are written and running today.

### Memory System (`brain/memory_system/`)

Venv: `brain/memory_system/venv` | Run: `cd brain/memory_system && ./run_tests.sh`

| Test File | Tests | Level | What It Covers |
|-----------|-------|-------|----------------|
| `test_contact_matching.py` | 28 | Unit | Name normalization, similarity scoring (exact, prefix, nickname, reversed, single-name), `find_best_match()` threshold and tiebreaking |
| `test_contact_reconciliation.py` | 4 | Integration | Phase 4: links entities to identifiers, skips already linked, skips low mention entities, score consistency |
| `test_intelligence.py` | 32 | Unit + Integration | `continuous_ingest.py` (18), `periodic_analysis.py` (6), `intelligence_pipeline.py` (8) — dedup, watermarks, phase outputs |
| `test_phase1_fact_extraction.py` | — | LLM | Fact extraction from text |
| `test_phase2_conflict_resolution.py` | — | LLM | Fact conflict detection and resolution |
| `test_phase3_mcp_server.py` | — | Integration | MCP server tool registration and responses |
| `test_phase4_entity_graph.py` | — | Integration | Entity and relation extraction |
| `test_phase5_ingestion.py` | — | Integration | RAG ingestion pipeline |
| `test_phase6_lifecycle.py` | — | Integration | Memory TTL, decay, archival |
| `test_rag_serve.py` | — | LLM | RAG query → response quality |
| `test_ingest_service.py` | — | Integration | `/ingest` endpoint |
| `test_e2e_pipeline.py` | — | E2E | Full ingestion → retrieval flow |
| `test_stack.py` | — | Smoke | Service health checks |

### CRM (`crm/`)

| Test File | Tests | Level | What It Covers |
|-----------|-------|-------|----------------|
| `crm/bridge/tests/test_bridge_api.py` | ~15 | Unit | Health endpoint, resolve-contact, webhook ingestion, CRM proxy endpoints |
| `crm/bridge/tests/test_contact_resolver.py` | ~8 | Unit | Mapping lookup, gap-filling, new contact creation, upsert logic |
| `crm/tests/test_phase3_memory_blocks.py` | 18 | Integration | Memory block CRUD, size limits, timestamps (uses memory venv) |

### CRM Shell Tests (`crm/tests/`)

| Test File | Tests | What It Covers |
|-----------|-------|----------------|
| `test_phase0_prerequisites.sh` | 10 | PostgreSQL, Redis, Docker, networking prerequisites |
| `test_phase1_services.sh` | 13 | Docker containers, systemd services, health endpoints |
| `test_phase4_mcp.sh` | 11 | MCP server tool listing and invocation |
| `test_regression.sh` | 20 | Full system regression (all services, containers, endpoints) |
| `test_email_pipeline.sh` | — | Email pipeline integration |

---

## Priority Coverage Plan

### Phase 1 (Week 1-2): Core Service Smoke + Unit Tests — DONE

| Component | Test File | Tests | What They Assert | Status |
|-----------|-----------|-------|------------------|--------|
| Bridge API | `crm/bridge/tests/test_bridge_api.py` | ~15 | Health endpoint, resolve-contact validation, webhook ingestion, CRM proxy endpoints, log-interaction | Done |
| Contact Resolver | `crm/bridge/tests/test_contact_resolver.py` | ~8 | Existing mapping lookup, gap-filling, new contact creation, upsert COALESCE logic, timeline | Done |
| Contact Matching | `brain/memory_system/test_contact_matching.py` | 28 | Name normalization, similarity scoring (exact, prefix, nickname, reversed, three-part), threshold, tiebreaking | Done |
| Contact Reconciliation | `brain/memory_system/test_contact_reconciliation.py` | 4 | Phase 4 entity↔identifier linking, skip already-linked, skip low-mention, score consistency | Done |
| Intelligence Pipeline | `brain/memory_system/test_intelligence.py` | 32 | Continuous ingest (18), periodic analysis (6), intelligence pipeline (8) — dedup, watermarks, phases | Done |
| Orchestrator (unit) | `brain/memory_system/tests/test_orchestrator_unit.py` | ~8 | classify_query categories, format_merged_context truncation + ordering, RAG_PROFILES schema | TODO |

**Fixtures needed:** `test_client` (ASGI), `mock_http_client`, `test_prefix`, `db_conn`, `cleanup_test_data`
**Mocks:** `bridge_service.http_client`, `contact_resolver.get_db`, `crm_dal.*`

### Phase 2 (Week 3-4): Communication Layer

| Component | Test File | Tests | What They Assert |
|-----------|-----------|-------|------------------|
| Voice webhooks | `brain/voice-server/tests/test_voice_webhooks.py` | ~8 | Twilio signature validation, TwiML response format, ConversationRelay events |
| Contact resolver (full) | `crm/bridge/tests/test_contact_resolver_integration.py` | ~6 | Real DB upserts, cross-system search with mocked APIs |
| CRM data sync | `crm/tests/test_crm_sync.py` | ~5 | CRM contact consistency via crm_dal, duplicate detection |

**Fixtures needed:** `db_conn` (real), `mock_twilio_signature`, sample webhook payloads
**Mocks:** Twilio HTTP, crm_dal

### Phase 3 (Week 5-6): MCP + Plugin Tools

| Component | Test File | Tests | What They Assert |
|-----------|-----------|-------|------------------|
| CRM MCP tools | `brain/memory_system/test_phase3_mcp_server.py` | ~10 | Tool schema validation, argument mapping, CRM CRUD via crm_dal |
| OpenClaw CRM plugin | `runtime/extensions/crm-tools/tests/test_crm_plugin.py` | ~8 | Plugin registration, fetch→Bridge proxy calls, error handling |

**Fixtures needed:** Mock MCP server context, mock Bridge HTTP
**Mocks:** HTTP responses from Bridge, crm_dal

### Phase 4 (Week 7-8): AI Quality

| Component | Test File | Tests | What They Assert |
|-----------|-----------|-------|------------------|
| Triage worker | `brain/tests/test_triage_golden.py` | ~8 | Golden dataset: known inputs → correct categorization (>=80%) |
| Vision accuracy | `brain/tests/test_vision_accuracy.py` | ~5 | Object detection on test images, face enrollment/recognition |
| Fact extraction | `brain/memory_system/tests/test_fact_quality.py` | ~6 | Multi-fact text → >=3 facts, correct categories, entity linking |

**Fixtures needed:** `mock_llm`, golden dataset files, test images
**Mocks:** Ollama API (for unit), real Ollama (for @llm tests)

### Phase 5 (Week 9-10): Cron + Maintenance

| Component | Test File | Tests | What They Assert |
|-----------|-----------|-------|------------------|
| Calendar sync | `brain/scripts/tests/test_calendar_edge_cases.py` | ~6 | Timezone handling, all-day events, recurring events, deleted events |
| CRM consistency | `crm/tests/test_crm_consistency.py` | ~5 | Orphan detection, stale mapping cleanup, cross-system ID validation |
| Weekly review | `brain/memory_system/tests/test_weekly_review.py` | ~5 | Output file structure, date range calculation, section headers |

**Fixtures needed:** `freezegun` for date mocking, sample log files
**Mocks:** Calendar API, file system for log reading

### Phase 6 (Week 11-12): End-to-End Flows

| Component | Test File | Tests | What They Assert |
|-----------|-----------|-------|------------------|
| Email→notification | `tests/e2e/test_email_to_notification.py` | ~3 | email_sync → triage → Telegram delivery (mock Telegram API) |
| Voice→memory | `tests/e2e/test_voice_to_memory.py` | ~3 | Twilio webhook → transcription → memory ingestion → fact search |
| Conversation flow | `tests/e2e/test_conversation_flow.py` | ~3 | Incoming message → Bridge → contact resolution → memory → response |

**Fixtures needed:** Full service mocks or real services, test data builders
**Mocks:** External APIs (Telegram, Twilio), or use real services with test accounts

---

## Running Tests

```bash
# All fast tests (pre-commit)
pytest -m "not slow and not llm and not e2e"

# Bridge tests only
cd /home/philip/robothor && crm/bridge/venv/bin/pytest crm/bridge/tests/ -v

# Memory system tests (uses its own venv)
cd /home/philip/robothor/brain/memory_system && ./run_tests.sh

# Contact matching tests only (fast, pure unit tests)
cd /home/philip/robothor/brain/memory_system && ./venv/bin/pytest test_contact_matching.py -v

# Contact reconciliation tests (integration, needs DB)
cd /home/philip/robothor/brain/memory_system && ./venv/bin/pytest test_contact_reconciliation.py -v

# Intelligence pipeline tests (continuous ingest + periodic analysis + intelligence pipeline)
cd /home/philip/robothor/brain/memory_system && ./venv/bin/pytest test_intelligence.py -v

# CRM shell tests
bash /home/philip/robothor/crm/tests/test_regression.sh

# Unified runner (all modules)
bash /home/philip/robothor/run_tests.sh

# Full suite including LLM tests
bash /home/philip/robothor/run_tests.sh --all
```
