#!/usr/bin/env python3
"""
One-time migration: re-embed all rows from 768-dim (nomic-embed-text) to
1024-dim (qwen3-embedding:0.6b).

Steps:
  1. Add embedding_new vector(1024) column to both tables
  2. Re-embed all rows via qwen3-embedding:0.6b
  3. Drop old embedding column, rename embedding_new → embedding
  4. Recreate IVFFlat indexes
  5. Verify dimensions + test search

Usage:
  cd /home/philip/clawd/memory_system
  source venv/bin/activate
  python3 migrate_embeddings.py

Rollback:
  If embedding_new exists but old embedding hasn't been dropped yet,
  just: ALTER TABLE short_term_memory DROP COLUMN embedding_new;
  Or restore from pg_dump backup.
"""

import sys
import time

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

OLLAMA_URL = "http://localhost:11434"
EMBEDDING_MODEL = "qwen3-embedding:0.6b"

DB_CONFIG = {"dbname": "robothor_memory", "user": "philip", "host": "/var/run/postgresql"}


def get_embedding(text: str) -> list:
    """Get 1024-dim embedding from qwen3-embedding:0.6b via /api/embed."""
    response = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["embeddings"][0]


def migrate():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=" * 60)
    print("  Embedding Migration: 768-dim → 1024-dim")
    print("=" * 60)

    # --- Step 0: Check current state ---
    cur.execute("SELECT COUNT(*) as count FROM short_term_memory")
    short_count = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) as count FROM long_term_memory")
    long_count = cur.fetchone()["count"]
    print(f"\nRows to migrate: {short_count} short-term, {long_count} long-term")
    total = short_count + long_count

    if total == 0:
        print("No rows to migrate. Updating schema only...")

    # --- Step 1: Add new column ---
    print("\n[1/5] Adding embedding_new vector(1024) columns...")
    try:
        cur.execute("ALTER TABLE short_term_memory ADD COLUMN embedding_new vector(1024)")
        cur.execute("ALTER TABLE long_term_memory ADD COLUMN embedding_new vector(1024)")
        conn.commit()
        print("  Done.")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
        print("  Columns already exist (resuming previous migration).")

    # --- Step 2: Re-embed all rows ---
    print(f"\n[2/5] Re-embedding {total} rows with {EMBEDDING_MODEL}...")
    t0 = time.time()
    migrated = 0
    errors = 0

    # Short-term memory
    cur.execute("SELECT id, content FROM short_term_memory WHERE embedding_new IS NULL ORDER BY id")
    rows = cur.fetchall()
    for row in rows:
        try:
            emb = get_embedding(row["content"])
            cur.execute(
                "UPDATE short_term_memory SET embedding_new = %s WHERE id = %s",
                (emb, row["id"]),
            )
            migrated += 1
            if migrated % 50 == 0:
                conn.commit()
                elapsed = time.time() - t0
                rate = migrated / elapsed if elapsed > 0 else 0
                print(f"  {migrated}/{total} ({rate:.1f} rows/s)")
        except Exception as e:
            errors += 1
            print(f"  ERROR on short_term id={row['id']}: {e}", file=sys.stderr)

    # Long-term memory
    cur.execute(
        "SELECT id, COALESCE(summary, content) as text FROM long_term_memory WHERE embedding_new IS NULL ORDER BY id"
    )
    rows = cur.fetchall()
    for row in rows:
        try:
            emb = get_embedding(row["text"])
            cur.execute(
                "UPDATE long_term_memory SET embedding_new = %s WHERE id = %s",
                (emb, row["id"]),
            )
            migrated += 1
            if migrated % 50 == 0:
                conn.commit()
                elapsed = time.time() - t0
                rate = migrated / elapsed if elapsed > 0 else 0
                print(f"  {migrated}/{total} ({rate:.1f} rows/s)")
        except Exception as e:
            errors += 1
            print(f"  ERROR on long_term id={row['id']}: {e}", file=sys.stderr)

    conn.commit()
    elapsed = time.time() - t0
    print(f"  Done: {migrated} migrated, {errors} errors in {elapsed:.1f}s")

    if errors > 0:
        print(f"\n  WARNING: {errors} rows failed. Fix errors and re-run (idempotent).")
        print("  Rows with embedding_new IS NULL will be retried.")
        resp = input("  Continue with schema swap? [y/N] ")
        if resp.lower() != "y":
            print("  Aborted. Run again after fixing errors.")
            conn.close()
            return

    # --- Step 3: Drop old, rename new ---
    print("\n[3/5] Swapping columns (drop old embedding, rename embedding_new → embedding)...")

    # Drop indexes first
    cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE tablename IN ('short_term_memory', 'long_term_memory')
          AND indexdef LIKE '%embedding%'
    """)
    for row in cur.fetchall():
        idx = row["indexname"]
        print(f"  Dropping index: {idx}")
        cur.execute(f"DROP INDEX IF EXISTS {idx}")

    cur.execute("ALTER TABLE short_term_memory DROP COLUMN embedding")
    cur.execute("ALTER TABLE short_term_memory RENAME COLUMN embedding_new TO embedding")
    cur.execute("ALTER TABLE long_term_memory DROP COLUMN embedding")
    cur.execute("ALTER TABLE long_term_memory RENAME COLUMN embedding_new TO embedding")
    conn.commit()
    print("  Done.")

    # --- Step 4: Recreate indexes ---
    print("\n[4/5] Recreating IVFFlat indexes...")

    # Determine list counts based on row counts
    short_lists = max(1, min(short_count // 50, 10))
    long_lists = max(1, min(long_count // 10, 5))

    cur.execute(f"""
        CREATE INDEX idx_short_term_embedding ON short_term_memory
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = {short_lists})
    """)
    print(f"  short_term_memory: IVFFlat with {short_lists} lists")

    cur.execute(f"""
        CREATE INDEX idx_long_term_embedding ON long_term_memory
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = {long_lists})
    """)
    print(f"  long_term_memory: IVFFlat with {long_lists} lists")

    conn.commit()
    print("  Done.")

    # --- Step 5: Verify ---
    print("\n[5/5] Verifying migration...")

    # Check dimensions
    cur.execute("""
        SELECT atttypmod FROM pg_attribute
        JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
        WHERE relname = 'short_term_memory' AND attname = 'embedding'
    """)
    row = cur.fetchone()
    dim = row["atttypmod"] if row else "unknown"
    print(f"  short_term_memory.embedding dimension: {dim}")

    cur.execute("""
        SELECT atttypmod FROM pg_attribute
        JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
        WHERE relname = 'long_term_memory' AND attname = 'embedding'
    """)
    row = cur.fetchone()
    dim = row["atttypmod"] if row else "unknown"
    print(f"  long_term_memory.embedding dimension: {dim}")

    # Test search
    if short_count > 0:
        test_emb = get_embedding("test query")
        cur.execute(
            """
            SELECT id, 1 - (embedding <=> %s::vector) as similarity
            FROM short_term_memory
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT 3
        """,
            (test_emb, test_emb),
        )
        results = cur.fetchall()
        print(f"  Test search returned {len(results)} results")
        for r in results:
            print(f"    id={r['id']}, similarity={r['similarity']:.4f}")

    conn.close()

    print("\n" + "=" * 60)
    print("  Migration complete!")
    print("=" * 60)


if __name__ == "__main__":
    migrate()
