"""
Shared test fixtures for the Robothor Memory System test suite.

Provides database connections, test data isolation via unique prefixes,
and automatic cleanup of test data after each test.
"""

import uuid

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}

# All tables that may contain test data
TEST_TABLES = [
    "memory_facts",
    "memory_entities",
    "memory_relations",
]


@pytest.fixture
def test_prefix():
    """Unique prefix to tag all test data for isolation and cleanup."""
    return f"__test_{uuid.uuid4().hex[:8]}__"


@pytest.fixture
def db_conn():
    """PostgreSQL connection for test use."""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    yield conn
    conn.close()


@pytest.fixture
def db_cursor(db_conn):
    """PostgreSQL cursor with RealDictCursor for test use."""
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    yield cur
    cur.close()


@pytest.fixture(autouse=True)
def cleanup_test_data(test_prefix, db_conn):
    """Autouse fixture that cleans up all test data after each test.

    Deletes rows from all test tables where content or name contains
    the test prefix. Runs after every test automatically.
    """
    yield

    cur = db_conn.cursor()
    try:
        # Clean memory_relations first (FK references)
        cur.execute(
            """
            DELETE FROM memory_relations
            WHERE fact_id IN (
                SELECT id FROM memory_facts
                WHERE fact_text LIKE %s OR source_content LIKE %s
            )
        """,
            (f"%{test_prefix}%", f"%{test_prefix}%"),
        )
    except psycopg2.errors.UndefinedTable:
        db_conn.rollback()

    try:
        cur.execute(
            """
            DELETE FROM memory_entities
            WHERE name LIKE %s OR %s = ANY(aliases)
        """,
            (f"%{test_prefix}%", test_prefix),
        )
    except psycopg2.errors.UndefinedTable:
        db_conn.rollback()

    try:
        # Nullify self-referential FK before deleting
        cur.execute(
            """
            UPDATE memory_facts SET superseded_by = NULL
            WHERE superseded_by IN (
                SELECT id FROM memory_facts
                WHERE fact_text LIKE %s OR source_content LIKE %s
            )
        """,
            (f"%{test_prefix}%", f"%{test_prefix}%"),
        )
        cur.execute(
            """
            DELETE FROM memory_facts
            WHERE fact_text LIKE %s OR source_content LIKE %s
        """,
            (f"%{test_prefix}%", f"%{test_prefix}%"),
        )
    except psycopg2.errors.UndefinedTable:
        db_conn.rollback()

    db_conn.commit()
    cur.close()


@pytest.fixture
def sample_content():
    """Pre-built test content for various test scenarios."""
    return {
        "conversation": (
            "Philip mentioned he's switching from VS Code to Neovim for "
            "Python development. He said the LSP integration is much better "
            "and he prefers the modal editing style. He's been using it for "
            "about two weeks now."
        ),
        "email": (
            "From: samantha@ironsailpharma.com\n"
            "To: philip@ironsail.ai\n"
            "Subject: Dinner plans for Friday\n\n"
            "Hey, let's do Italian this Friday at 7pm. I booked a table "
            "at Lucia's on Main Street. See you there!"
        ),
        "preference": (
            "Philip prefers dark mode for all applications. He uses the "
            "Catppuccin Mocha color scheme across his terminal, editor, "
            "and browser."
        ),
        "contradiction_pair": (
            "Philip's favorite programming language is Rust.",
            "Philip's favorite programming language is Python.",
        ),
        "technical": (
            "The Robothor system runs on an NVIDIA DGX Spark with 128GB "
            "unified memory. The primary LLM is Qwen3-Next-80B running "
            "via Ollama at approximately 45 tokens per second."
        ),
        "multi_fact": (
            "Philip decided to use PostgreSQL with pgvector for the memory "
            "system. He chose Qwen3-Next-80B as the primary LLM because of "
            "its strong reasoning capabilities. The system runs on an NVIDIA "
            "DGX Spark with 128GB unified memory. Philip's wife Samantha "
            "works at Ironsail Pharma."
        ),
    }
