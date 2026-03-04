#!/usr/bin/env python3
"""
Robothor Memory Service - Comprehensive audit logging and vector search

This service:
1. Logs ALL interactions to PostgreSQL audit_log
2. Vectorizes content for semantic search
3. Provides unified query interface

Usage:
  ./memory_service.py log <event_type> <action> [--details '{}'] [--category X]
  ./memory_service.py search <query> [--limit N]
  ./memory_service.py conversation <role> <content> [--session X]
  ./memory_service.py audit [--since YYYY-MM-DD] [--type X] [--limit N]
  ./memory_service.py stats
  ./memory_service.py vectorize <text> <type>
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import Json, RealDictCursor

# Database config - use peer auth via sudo
DB_NAME = "robothor_memory"


def get_conn():
    """Get database connection."""
    return psycopg2.connect(
        dbname=DB_NAME,
        user=os.getenv("PG_USER", "postgres"),
        password=os.environ["PG_PASSWORD"],
        host=os.getenv("PG_HOST", "127.0.0.1"),
    )


def get_embedding(text: str) -> list[float] | None:
    """Get embedding from Ollama qwen3-embedding:0.6b."""
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "http://localhost:11434/api/embed",
                "-d",
                json.dumps({"model": "qwen3-embedding:0.6b", "input": text}),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        embeddings = data.get("embeddings")
        if embeddings and len(embeddings) > 0:
            return embeddings[0]
        return None
    except Exception as e:
        print(f"Embedding error: {e}", file=sys.stderr)
        return None


# ============ AUDIT LOG ============


def log_event(
    event_type: str,
    action: str,
    category: str | None = None,
    details: dict | None = None,
    source_channel: str | None = None,
    target: str | None = None,
    status: str = "ok",
    actor: str = "robothor",
    session_key: str | None = None,
) -> dict:
    """Log an audit event."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO audit_log 
        (event_type, category, actor, action, details, source_channel, target, status, session_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, timestamp
    """,
        (
            event_type,
            category,
            actor,
            action,
            Json(details) if details else None,
            source_channel,
            target,
            status,
            session_key,
        ),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return {"id": row[0], "timestamp": str(row[1])}


def query_audit(
    limit: int = 50,
    event_type: str | None = None,
    category: str | None = None,
    since: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Query audit log."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query = """
        SELECT id, timestamp, event_type, category, actor, action, 
               details, source_channel, target, status, session_key 
        FROM audit_log WHERE 1=1
    """
    params = []

    if event_type:
        query += " AND event_type = %s"
        params.append(event_type)
    if category:
        query += " AND category = %s"
        params.append(category)
    if since:
        query += " AND timestamp >= %s"
        params.append(since)
    if search:
        query += " AND (action ILIKE %s OR details::text ILIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY timestamp DESC LIMIT %s"
    params.append(limit)

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [dict(r) for r in rows]


# ============ VECTOR MEMORY ============


def store_memory(
    content: str, content_type: str, metadata: dict | None = None, ttl_hours: int | None = 48
) -> dict:
    """Store content with vector embedding."""
    embedding = get_embedding(content)
    if not embedding:
        return {"error": "Failed to generate embedding"}

    conn = get_conn()
    cur = conn.cursor()

    expires_at = None
    if ttl_hours:
        expires_at = datetime.now() + timedelta(hours=ttl_hours)

    cur.execute(
        """
        INSERT INTO short_term_memory 
        (content, content_type, embedding, metadata, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, created_at
    """,
        (content, content_type, embedding, Json(metadata) if metadata else Json({}), expires_at),
    )

    row = cur.fetchone()
    conn.commit()
    conn.close()

    return {"id": row[0], "created_at": str(row[1])}


def search_memory(
    query: str, limit: int = 10, content_type: str | None = None, min_score: float = 0.5
) -> list[dict]:
    """Semantic search across memory."""
    embedding = get_embedding(query)
    if not embedding:
        return []

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Vector similarity search
    sql = """
        SELECT id, content, content_type, metadata, created_at,
               1 - (embedding <=> %s::vector) as similarity
        FROM short_term_memory
        WHERE (expires_at IS NULL OR expires_at > NOW())
    """
    params = [embedding]

    if content_type:
        sql += " AND content_type = %s"
        params.append(content_type)

    sql += """
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    params.extend([embedding, limit])

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    # Filter by minimum score and update access
    results = []
    for r in rows:
        if r["similarity"] >= min_score:
            results.append(
                {
                    "id": r["id"],
                    "content": r["content"],
                    "type": r["content_type"],
                    "metadata": r["metadata"],
                    "created_at": str(r["created_at"]),
                    "score": round(r["similarity"], 4),
                }
            )

    return results


# ============ CONVERSATION LOGGING ============


def log_conversation(
    role: str,  # 'user' or 'assistant'
    content: str,
    session_key: str | None = None,
    channel: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Log a conversation turn with both audit and vector storage."""

    # 1. Log to audit
    audit_result = log_event(
        event_type="conversation",
        action=content[:500],  # Truncate for audit
        category=role,
        details=metadata,
        source_channel=channel,
        session_key=session_key,
    )

    # 2. Store in vector memory for search
    full_metadata = {
        "role": role,
        "session_key": session_key,
        "channel": channel,
        "audit_id": audit_result["id"],
        **(metadata or {}),
    }

    memory_result = store_memory(
        content=content,
        content_type=f"conversation_{role}",
        metadata=full_metadata,
        ttl_hours=None,  # Conversations don't expire
    )

    return {"audit": audit_result, "memory": memory_result}


# ============ STATISTICS ============


def get_stats() -> dict:
    """Get memory system statistics."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    stats = {}

    # Audit log stats
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(DISTINCT event_type) as event_types,
            COUNT(DISTINCT category) as categories,
            MIN(timestamp) as earliest,
            MAX(timestamp) as latest
        FROM audit_log
    """)
    stats["audit_log"] = dict(cur.fetchone())

    # Event type breakdown
    cur.execute("""
        SELECT event_type, COUNT(*) as count 
        FROM audit_log 
        GROUP BY event_type 
        ORDER BY count DESC
    """)
    stats["audit_by_type"] = {r["event_type"]: r["count"] for r in cur.fetchall()}

    # Short-term memory stats
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE expires_at IS NULL) as permanent,
            COUNT(*) FILTER (WHERE expires_at > NOW()) as active,
            COUNT(DISTINCT content_type) as content_types
        FROM short_term_memory
    """)
    stats["short_term_memory"] = dict(cur.fetchone())

    # Long-term memory stats
    cur.execute("""
        SELECT COUNT(*) as total FROM long_term_memory
    """)
    stats["long_term_memory"] = {"total": cur.fetchone()["total"]}

    conn.close()
    return stats


# ============ CLI ============


def main():
    parser = argparse.ArgumentParser(description="Robothor Memory Service")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # log command
    log_parser = subparsers.add_parser("log", help="Log an audit event")
    log_parser.add_argument("event_type", help="Event type")
    log_parser.add_argument("action", help="Action description")
    log_parser.add_argument("--category", help="Category")
    log_parser.add_argument("--details", help="JSON details")
    log_parser.add_argument("--channel", help="Source channel")
    log_parser.add_argument("--target", help="Target")
    log_parser.add_argument("--session", help="Session key")

    # search command
    search_parser = subparsers.add_parser("search", help="Semantic search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--type", help="Content type filter")
    search_parser.add_argument("--min-score", type=float, default=0.5)

    # conversation command
    conv_parser = subparsers.add_parser("conversation", help="Log conversation")
    conv_parser.add_argument("role", choices=["user", "assistant"])
    conv_parser.add_argument("content", help="Message content")
    conv_parser.add_argument("--session", help="Session key")
    conv_parser.add_argument("--channel", help="Channel")

    # audit command
    audit_parser = subparsers.add_parser("audit", help="Query audit log")
    audit_parser.add_argument("--limit", type=int, default=20)
    audit_parser.add_argument("--type", help="Event type filter")
    audit_parser.add_argument("--category", help="Category filter")
    audit_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    audit_parser.add_argument("--search", help="Text search")

    # vectorize command
    vec_parser = subparsers.add_parser("vectorize", help="Store with vector")
    vec_parser.add_argument("text", help="Text to store")
    vec_parser.add_argument("type", help="Content type")
    vec_parser.add_argument("--ttl", type=int, help="TTL in hours (none=permanent)")
    vec_parser.add_argument("--metadata", help="JSON metadata")

    # stats command
    subparsers.add_parser("stats", help="Show statistics")

    args = parser.parse_args()

    if args.command == "log":
        details = json.loads(args.details) if args.details else None
        result = log_event(
            args.event_type,
            args.action,
            category=args.category,
            details=details,
            source_channel=args.channel,
            target=args.target,
            session_key=args.session,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "search":
        results = search_memory(
            args.query, limit=args.limit, content_type=args.type, min_score=args.min_score
        )
        for r in results:
            print(f"[{r['score']:.3f}] ({r['type']}) {r['content'][:100]}...")
        if not results:
            print("No results found")

    elif args.command == "conversation":
        result = log_conversation(
            args.role, args.content, session_key=args.session, channel=args.channel
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "audit":
        results = query_audit(
            limit=args.limit,
            event_type=args.type,
            category=args.category,
            since=args.since,
            search=args.search,
        )
        for r in results:
            ts = str(r["timestamp"])[:19]
            print(f"[{ts}] {r['event_type']:15} | {r['action'][:60]}")
        print(f"\n{len(results)} events")

    elif args.command == "vectorize":
        metadata = json.loads(args.metadata) if args.metadata else None
        result = store_memory(args.text, args.type, metadata=metadata, ttl_hours=args.ttl)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "stats":
        stats = get_stats()
        print(json.dumps(stats, indent=2, default=str))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
