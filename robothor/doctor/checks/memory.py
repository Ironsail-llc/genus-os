"""The identity blocks the main agent reads on every turn are not frozen.

``persona`` and ``user_profile`` are read into every conversation (3,601 and
1,300 reads on this instance) and written by nobody: no platform job
maintains them, and on 2026-09-14 the operator found ``persona`` asserting a
model and a voice the fleet had not used for seven months. Memory that is
read that often and never refreshed does not fail loudly — it quietly tells
the agent things that are no longer true.

The platform cannot know what the truth is; the operator (or the agent, on
instruction) writes these blocks. What the platform CAN do is refuse to let
them rot silently. Not repairable by ``--fix`` for that reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:
    from robothor.doctor.context import DoctorContext

#: The blocks every main-agent turn reads as "who am I / who is the operator".
IDENTITY_BLOCKS = ("persona", "user_profile")

#: Older than this and the block is a liability, not a memory. Ninety days is
#: three model changes on this instance's history.
MAX_AGE_DAYS = 90

#: Below this the block is a placeholder, whatever its date says.
MIN_CHARS = 120


def _rows(ctx: DoctorContext) -> list[dict[str, Any]]:
    with ctx.db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT block_name, length(content), last_written_at "
            "FROM agent_memory_blocks WHERE block_name = ANY(%s) AND pruned_at IS NULL",
            (list(IDENTITY_BLOCKS),),
        )
        return [
            {"block": str(row[0]), "chars": int(row[1] or 0), "written": row[2]}
            for row in cursor.fetchall()
        ]


def _age_days(written: Any, now: datetime) -> float | None:
    if written is None:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=UTC)
    return float((now - written).total_seconds()) / 86400


def _judge(rows: list[dict[str, Any]], now: datetime) -> list[str]:
    """The problems, one sentence each, naming the block and what to do."""
    by_name = {row["block"]: row for row in rows}
    problems: list[str] = []
    for name in IDENTITY_BLOCKS:
        row = by_name.get(name)
        if row is None:
            problems.append(f"{name}: missing — write it (memory_block_write)")
            continue
        age = _age_days(row["written"], now)
        if row["chars"] < MIN_CHARS:
            problems.append(f"{name}: {row['chars']} chars — a placeholder, write the real block")
        elif age is None:
            problems.append(f"{name}: never written — write it")
        elif age > MAX_AGE_DAYS:
            problems.append(
                f"{name}: last written {int(age)} days ago — reread it and rewrite what changed "
                "(models, voices and tools belong to the live engine state, not here)"
            )
    return problems


async def _identity_blocks(ctx: DoctorContext) -> Result:
    """``persona`` and ``user_profile`` exist, are not placeholders, and were
    written within the last ninety days."""
    try:
        rows = await ctx.run_blocking(lambda: _rows(ctx))
    except Exception as error:  # noqa: BLE001 - a missing table or database is a skip, not a crash
        return skip(f"could not read agent_memory_blocks ({type(error).__name__})")
    problems = _judge(rows, datetime.now(UTC))
    if problems:
        return fail("; ".join(problems))
    return ok(f"{len(IDENTITY_BLOCKS)} identity blocks written within {MAX_AGE_DAYS} days")


CHECKS: tuple[Check, ...] = (
    Check(
        id="memory.identity_blocks",
        title="The agent's identity blocks are current",
        category="memory",
        severity="recommended",
        run=_identity_blocks,
    ),
)
