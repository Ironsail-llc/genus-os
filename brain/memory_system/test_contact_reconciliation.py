"""Integration tests for contact reconciliation (Phase 4 of periodic_analysis).

Requires PostgreSQL with robothor_memory database.
Uses test_prefix isolation from conftest.py for cleanup.
"""

import pytest
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}


@pytest.fixture
def setup_test_entity(test_prefix, db_conn):
    """Create a test memory entity and return its ID."""
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        INSERT INTO memory_entities (name, entity_type, mention_count)
        VALUES (%s, 'person', 25)
        RETURNING id
    """,
        (f"{test_prefix}Philip D'Agostino",),
    )
    entity_id = cur.fetchone()["id"]
    db_conn.commit()
    return entity_id


@pytest.fixture
def setup_test_identifier(test_prefix, db_conn):
    """Create a test contact_identifiers row with NULL memory_entity_id."""
    cur = db_conn.cursor()
    cur.execute(
        """
        INSERT INTO contact_identifiers (channel, identifier, display_name)
        VALUES (%s, %s, %s)
        RETURNING id
    """,
        (
            "email",
            f"{test_prefix}@test.com",
            f"{test_prefix}Philip D'Agostino",
        ),
    )
    row_id = cur.fetchone()[0]
    db_conn.commit()
    return row_id


@pytest.fixture(autouse=True)
def cleanup_contact_identifiers(test_prefix, db_conn):
    """Clean up contact_identifiers rows created during tests."""
    yield
    cur = db_conn.cursor()
    cur.execute(
        "DELETE FROM contact_identifiers WHERE display_name LIKE %s OR identifier LIKE %s",
        (f"%{test_prefix}%", f"%{test_prefix}%"),
    )
    db_conn.commit()


@pytest.mark.integration
class TestContactReconciliation:
    def test_links_entity_to_identifier(
        self, test_prefix, db_conn, setup_test_entity, setup_test_identifier
    ):
        """Verify that fuzzy matching links a memory entity to a contact_identifier row."""
        from contact_matching import find_best_match

        cur = db_conn.cursor(cursor_factory=RealDictCursor)

        # Fetch unlinked identifiers
        cur.execute(
            """
            SELECT id, display_name FROM contact_identifiers
            WHERE memory_entity_id IS NULL AND display_name LIKE %s
        """,
            (f"%{test_prefix}%",),
        )
        unlinked = cur.fetchall()
        assert len(unlinked) == 1

        # Fetch person entities
        cur.execute(
            """
            SELECT id, name, entity_type, mention_count
            FROM memory_entities WHERE entity_type = 'person' AND name LIKE %s
        """,
            (f"%{test_prefix}%",),
        )
        entities = cur.fetchall()
        assert len(entities) == 1

        # Run matching
        match = find_best_match(
            unlinked[0]["display_name"],
            entities,
            threshold=0.75,
        )
        assert match is not None
        assert match["match_score"] >= 0.75

        # Apply the link
        cur.execute(
            """
            UPDATE contact_identifiers
            SET memory_entity_id = %s, updated_at = NOW()
            WHERE id = %s AND memory_entity_id IS NULL
        """,
            (match["id"], unlinked[0]["id"]),
        )
        db_conn.commit()

        # Verify
        cur.execute(
            """
            SELECT memory_entity_id FROM contact_identifiers WHERE id = %s
        """,
            (unlinked[0]["id"],),
        )
        result = cur.fetchone()
        assert result["memory_entity_id"] == setup_test_entity

    def test_skips_already_linked(self, test_prefix, db_conn, setup_test_entity):
        """Verify that already-linked identifiers are not re-processed."""
        cur = db_conn.cursor(cursor_factory=RealDictCursor)

        # Create an already-linked identifier
        cur.execute(
            """
            INSERT INTO contact_identifiers
                (channel, identifier, display_name, memory_entity_id)
            VALUES (%s, %s, %s, %s)
        """,
            (
                "email",
                f"{test_prefix}linked@test.com",
                f"{test_prefix}Already Linked",
                setup_test_entity,
            ),
        )
        db_conn.commit()

        # Query for unlinked only
        cur.execute(
            """
            SELECT COUNT(*) as cnt FROM contact_identifiers
            WHERE memory_entity_id IS NULL AND display_name LIKE %s
        """,
            (f"%{test_prefix}%",),
        )
        assert cur.fetchone()["cnt"] == 0

    def test_skips_low_mention_entities(self, test_prefix, db_conn):
        """Verify that entities with low mention counts are not discovered."""
        cur = db_conn.cursor(cursor_factory=RealDictCursor)

        # Create low-mention entity
        cur.execute(
            """
            INSERT INTO memory_entities (name, entity_type, mention_count)
            VALUES (%s, 'person', 2)
            RETURNING id
        """,
            (f"{test_prefix}Low Mention Person",),
        )
        db_conn.commit()

        # Query for high-mention discovery candidates
        cur.execute(
            """
            SELECT id, name FROM memory_entities
            WHERE entity_type = 'person'
              AND mention_count >= 5
              AND name LIKE %s
        """,
            (f"%{test_prefix}%",),
        )
        assert len(cur.fetchall()) == 0

    def test_matching_scores_are_consistent(self):
        """Verify matching is deterministic and produces consistent scores."""
        from contact_matching import name_similarity

        # Run the same comparison multiple times
        scores = [name_similarity("Philip D'Agostino", "Philip D'Agostino") for _ in range(10)]
        assert all(s == 1.0 for s in scores)

        scores = [name_similarity("Rochelle", "Rochelle Blaza") for _ in range(10)]
        assert all(s == 0.8 for s in scores)
