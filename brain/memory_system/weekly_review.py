#!/usr/bin/env python3
"""
Weekly Deep Review for Robothor.

Runs Sunday 5:00 AM (after data archival at 4:00).
Llama generates a comprehensive weekly review of all activity.

Outputs:
- memory/weekly-review-YYYY-MM-DD.md (human-readable)
- High-importance facts ingested into long-term memory

Model: Llama 3.2 Vision 11B (local, zero API cost).
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
MEMORY_SYSTEM_DIR = Path("/home/philip/robothor/brain/memory_system")
LOGS_DIR = MEMORY_SYSTEM_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}


async def load_llm_client():
    from robothor.llm import ollama as llm_client

    return llm_client


async def gather_weekly_data() -> dict:
    """Gather all data from the past 7 days."""
    data = {
        "facts_by_category": {},
        "email_count": 0,
        "conversation_count": 0,
        "tasks_completed": 0,
    }

    # Facts from the past week, grouped by category
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT category, fact_text, source_channel, created_at
            FROM memory_facts
            WHERE created_at > NOW() - INTERVAL '7 days'
              AND is_active = TRUE
            ORDER BY category, created_at DESC
        """)
        for row in cur.fetchall():
            cat = row["category"] or "uncategorized"
            data["facts_by_category"].setdefault(cat, []).append(
                {
                    "text": row["fact_text"],
                    "channel": row["source_channel"],
                    "date": row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "",
                }
            )

        # Count emails processed
        cur.execute("""
            SELECT COUNT(*) FROM memory_facts
            WHERE source_channel = 'email'
              AND created_at > NOW() - INTERVAL '7 days'
        """)
        data["email_count"] = cur.fetchone()[0]

        # Count CRM conversations
        cur.execute("""
            SELECT COUNT(*) FROM memory_facts
            WHERE source_channel = 'conversation'
              AND created_at > NOW() - INTERVAL '7 days'
        """)
        data["conversation_count"] = cur.fetchone()[0]

        # Completed tasks
        cur.execute("""
            SELECT COUNT(*) FROM memory_facts
            WHERE source_type = 'decision'
              AND fact_text LIKE 'Task Completed:%'
              AND created_at > NOW() - INTERVAL '7 days'
        """)
        data["tasks_completed"] = cur.fetchone()[0]

        conn.close()
    except Exception as e:
        logger.error("Failed to gather weekly data: %s", e)

    return data


async def main():
    start_time = datetime.now()
    today = start_time.strftime("%Y-%m-%d")
    logger.info("=== Weekly Deep Review Started: %s ===", start_time)

    llm_client = await load_llm_client()

    # Gather data
    logger.info("Gathering weekly data...")
    data = await gather_weekly_data()

    # Build input for Llama
    sections = []
    total_facts = 0
    for category, facts in sorted(data["facts_by_category"].items()):
        total_facts += len(facts)
        fact_lines = [f"  - [{f['channel']}] {f['text'][:150]}" for f in facts[:15]]
        sections.append(f"{category.upper()} ({len(facts)} facts):\n" + "\n".join(fact_lines))

    stats_line = (
        f"Week stats: {total_facts} facts, {data['email_count']} emails, "
        f"{data['conversation_count']} conversations, {data['tasks_completed']} tasks completed"
    )
    sections.insert(0, stats_line)

    input_text = "\n\n".join(sections)

    # Generate review
    logger.info("Generating weekly review with Llama (%d facts)...", total_facts)
    review = await llm_client.generate(
        prompt=f"""Generate a comprehensive weekly review for the week ending {today}. Cover:

1. **Key Decisions Made** — what was decided this week
2. **Contacts & Relationships** — who was interacted with, engagement trends
3. **Projects & Tasks** — progress made, what's outstanding
4. **Patterns & Themes** — recurring topics, emerging trends
5. **Action Items** — things that need attention next week
6. **Highlights** — notable events or achievements

Data from the past 7 days:
{input_text}

Write a well-structured review in markdown format. Be specific with names, dates, and details.""",
        system="You are writing a weekly intelligence review. Be thorough but organized.",
        temperature=0.3,
        max_tokens=3000,
    )

    # Save as markdown file
    review_path = MEMORY_DIR / f"weekly-review-{today}.md"
    review_content = (
        f"# Weekly Review — {today}\n\n_Generated by Robothor Intelligence Pipeline_\n\n{review}"
    )
    review_path.write_text(review_content)
    logger.info("Saved review to %s (%d chars)", review_path, len(review_content))

    # Extract key findings and ingest as high-importance facts
    logger.info("Extracting key findings for long-term memory...")
    findings = await llm_client.generate(
        prompt=f"""From this weekly review, extract 3-5 key findings that should be remembered long-term.
Each finding should be a single, self-contained sentence.

Review:
{review[:3000]}

Output one finding per line, no numbering or bullets.""",
        system="Extract key findings. One sentence each.",
        temperature=0.3,
        max_tokens=500,
    )

    ingested = 0
    for line in findings.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            from robothor.memory.ingestion import ingest_content

            await ingest_content(
                content=f"Weekly Review Finding ({today}): {line}",
                source_channel="crm",
                content_type="decision",
                metadata={
                    "type": "weekly_review_finding",
                    "week_ending": today,
                    "importance": 0.9,
                },
            )
            ingested += 1
        except Exception as e:
            logger.warning("Failed to ingest finding: %s", e)

    duration = (datetime.now() - start_time).total_seconds()
    logger.info("=== Weekly Review Complete (%.1fs) — %d findings ingested ===", duration, ingested)

    print(f"Weekly Review — {today}")
    print(f"  Facts analyzed: {total_facts}")
    print(f"  Review saved: {review_path}")
    print(f"  Findings ingested: {ingested}")
    print(f"  Duration: {duration:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
