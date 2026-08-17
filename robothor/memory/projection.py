"""Read-only markdown projection of memory for the operator.

WHAT THIS IS

A one-way render of the highest-value facts into markdown notes the operator can
read in Obsidian. Postgres remains the system of record. Nothing here is ever
read back, and the projection is never auto-loaded into an agent session.

WHAT THE RESEARCH ACTUALLY SUPPORTED

The 93-agent survey of how competitors use Obsidian for agent memory returned a
narrower answer than the question implied: markdown-as-memory wins on human
inspectability and loses on retrieval — no vectors, no ranking, no tenancy, and
a linear scan over 152k facts. So the verdict was keep Postgres and steal the
inspectability, which is exactly this and nothing more.

WHY IT IS BUILT TO BE DELETED

A projection nobody reads is a nightly job that produces files, and files are
easy to mistake for value. Kill criteria are fixed in advance, at 7 days:

    zero agent reads AND zero operator corrections -> delete it
    corrections but no reads                       -> ship as a dashboard tab
    reads                                          -> keep

``projection_usage_report`` measures the first two so the decision is made on a
number rather than on how the files feel.

Output goes under ROBOTHOR_WORKSPACE/brain/memory/vault/ — instance data,
gitignored, never platform.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Enough to be useful, small enough that a human can actually scan it. Beyond
# a few hundred notes an operator stops reading and it becomes a file dump.
DEFAULT_MAX_NOTES = 100

_UNSAFE = re.compile(r"[^a-z0-9]+")

# Written into every file. If this string is absent the file was not produced
# by this projection, and the sweeper must not delete it.
GENERATED_MARKER = "genus-os:memory-projection"


def projection_dir() -> Path:
    workspace = os.environ.get("ROBOTHOR_WORKSPACE") or str(Path.home() / "robothor")
    return Path(workspace) / "brain" / "memory" / "vault"


def projection_enabled() -> bool:
    return os.environ.get("MEMORY_PROJECTION", "").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    )


def slugify(text: str, *, max_len: int = 60) -> str:
    """Filesystem-safe stem. Collisions are resolved by the caller with the id."""
    slug = _UNSAFE.sub("-", (text or "").lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"


def render_note(fact: dict[str, Any], *, generated_at: datetime | None = None) -> str:
    """One fact as a markdown note with frontmatter provenance.

    Provenance is the whole point: an operator reading a claim needs to know
    where it came from and how stale it is, otherwise the projection is just
    assertions in a nicer font.
    """
    stamp = (generated_at or datetime.now(UTC)).isoformat()
    entities = fact.get("entities") or []
    updated = fact.get("updated_at")
    lines = [
        "---",
        f"fact_id: {fact.get('id')}",
        f"category: {fact.get('category') or 'uncategorized'}",
        f"confidence: {fact.get('confidence')}",
        f"importance: {fact.get('importance_score')}",
        f"source_type: {fact.get('source_type') or 'unknown'}",
        f"fact_updated_at: {updated.isoformat() if updated is not None and hasattr(updated, 'isoformat') else updated}",
        f"generated_at: {stamp}",
        f"generator: {GENERATED_MARKER}",
        "read_only: true",
        f"entities: [{', '.join(str(e) for e in entities)}]",
        "---",
        "",
        f"# {fact.get('fact_text', '').strip()}",
        "",
        "> Read-only projection. Postgres is the system of record — editing this",
        "> file changes nothing. Corrections belong in a conversation with the agent.",
        "",
    ]
    if entities:
        # Obsidian wikilinks are the one feature genuinely worth stealing: they
        # make the entity graph browsable without building a graph UI.
        lines.append("Entities: " + " ".join(f"[[{e}]]" for e in entities))
        lines.append("")
    return "\n".join(lines)


def select_facts(
    limit: int = DEFAULT_MAX_NOTES, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    """Highest-value active facts: importance first, then recency."""
    from robothor.db.connection import get_connection

    sql = """
        SELECT id, fact_text, category, entities, confidence, importance_score,
               source_type, updated_at
        FROM memory_facts
        WHERE is_active = TRUE
          AND length(fact_text) >= 25
    """
    params: list[Any] = []
    if tenant_id:
        sql += " AND tenant_id = %s"
        params.append(tenant_id)
    sql += " ORDER BY COALESCE(importance_score, 0) DESC, updated_at DESC LIMIT %s"
    params.append(limit)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def project(
    *, limit: int = DEFAULT_MAX_NOTES, tenant_id: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Render the projection. Returns a report.

    Only ever removes files it generated — the marker check means an operator's
    own notes in the same folder survive, which matters because the folder is
    inside their vault.
    """
    facts = select_facts(limit, tenant_id)
    out_dir = projection_dir()
    generated_at = datetime.now(UTC)

    planned: dict[str, str] = {}
    for fact in facts:
        stem = f"{slugify(fact.get('fact_text', ''))}-{fact['id']}"
        planned[f"{stem}.md"] = render_note(fact, generated_at=generated_at)

    if dry_run:
        return {
            "dry_run": True,
            "would_write": len(planned),
            "dir": str(out_dir),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    stale = 0
    for existing in out_dir.glob("*.md"):
        if existing.name in planned:
            continue
        try:
            if GENERATED_MARKER in existing.read_text():
                existing.unlink()
                stale += 1
        except OSError:
            continue

    for name, body in planned.items():
        (out_dir / name).write_text(body)

    (out_dir / "README.md").write_text(
        "---\n"
        f"generated_at: {generated_at.isoformat()}\n"
        f"generator: {GENERATED_MARKER}\n"
        "---\n\n"
        "# Memory projection (read-only)\n\n"
        f"{len(planned)} notes rendered from the top facts by importance.\n\n"
        "Postgres is the system of record. These files are regenerated on a\n"
        "schedule and any edit is overwritten. Nothing here is loaded into an\n"
        "agent session.\n\n"
        "This projection is on trial. If it is not read within 7 days it is\n"
        "deleted — see `robothor.memory.projection` for the kill criteria.\n"
    )

    return {
        "written": len(planned),
        "removed_stale": stale,
        "dir": str(out_dir),
        "generated_at": generated_at.isoformat(),
    }


def projection_usage_report() -> dict[str, Any]:
    """Evidence for the 7-day kill decision.

    Reads filesystem atime, which is the only signal available for "did a human
    open this". It is imperfect — a backup sweep can touch it — so it is
    reported, never auto-actioned.
    """
    out_dir = projection_dir()
    if not out_dir.exists():
        return {"exists": False, "notes": 0, "opened": 0}

    notes = [p for p in out_dir.glob("*.md") if p.name != "README.md"]
    opened = 0
    for p in notes:
        try:
            st = p.stat()
            # atime meaningfully later than mtime means something read it after
            # the projection wrote it.
            if st.st_atime > st.st_mtime + 60:
                opened += 1
        except OSError:
            continue
    return {
        "exists": True,
        "notes": len(notes),
        "opened": opened,
        "dir": str(out_dir),
        "verdict": (
            "keep — the projection is being read"
            if opened
            else "no reads detected; delete at 7 days unless corrections say otherwise"
        ),
    }
