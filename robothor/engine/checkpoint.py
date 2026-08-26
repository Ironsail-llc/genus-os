"""
Checkpointing — mid-run state persistence for long-running agents.

Saves conversation state periodically to the agent_run_checkpoints table.
Supports resume: reload messages from the latest checkpoint and continue
the conversation loop where it left off.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_INTERVAL = 5  # checkpoint every N successful tool calls

#: Also checkpoint when this long has passed since the last one, regardless of
#: tool-call count. The step trigger alone cannot fire for long, low-step work,
#: which is exactly the work that most needs to survive a restart: measured over
#: 30 days on this instance, benchmark-runner averaged 107 minutes and 1.2 tool
#: calls per run, so it never reached 5 and never checkpointed. Of 45 of its
#: runs killed by a daemon restart, ZERO had a checkpoint to resume from.
#: Durability was inversely correlated with how much it was needed.
CHECKPOINT_MAX_SECONDS = 300.0
CHECKPOINT_SCHEMA_VERSION = 1  # increment when checkpoint format changes


@dataclass
class CheckpointManager:
    """Manages mid-run state snapshots."""

    run_id: str = ""
    interval: int = CHECKPOINT_INTERVAL
    max_seconds: float = CHECKPOINT_MAX_SECONDS
    clock: Callable[[], float] = time.monotonic
    _success_count: int = 0
    _checkpoint_count: int = 0
    _last_checkpoint_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        self._last_checkpoint_at = self.clock()

    def record_success(self) -> None:
        """Record a successful tool call."""
        self._success_count += 1

    def note_checkpoint_saved(self) -> None:
        """Rearm the time trigger. Without this every later iteration fires."""
        self._last_checkpoint_at = self.clock()

    def should_checkpoint(self) -> bool:
        """Whether it's time to save a checkpoint.

        Two triggers, either sufficient. The step trigger is the original: N
        successful tool calls. The time trigger exists because that one cannot
        fire for work that spends an hour inside a single tool call — the
        precise shape of every long-running agent here, and the reason resume
        had nothing to resume from.

        A run that has done no work at all is never checkpointed: there is
        nothing worth persisting, and the runner would otherwise write one on
        the first idle iteration of every run.
        """
        if self._success_count == 0:
            return False
        if self._success_count % self.interval == 0:
            return True
        return (self.clock() - self._last_checkpoint_at) >= self.max_seconds

    def save(
        self,
        step_number: int,
        messages: list[dict[str, Any]],
        scratchpad: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        todo_list: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a checkpoint to the database. Best-effort — never raises.

        Phase 5: ``todo_list`` is embedded under ``scratchpad["_todo_list"]``
        so resume can rebuild the in-conversation checklist. No schema bump —
        the scratchpad column is already JSONB and Scratchpad.from_dict
        tolerates unknown keys.
        """
        if todo_list is not None:
            scratchpad = dict(scratchpad or {})
            scratchpad["_todo_list"] = todo_list
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO agent_run_checkpoints
                        (run_id, step_number, messages, scratchpad, plan, schema_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self.run_id,
                        step_number,
                        json.dumps(messages, default=str),
                        json.dumps(scratchpad, default=str) if scratchpad else None,
                        json.dumps(plan, default=str) if plan else None,
                        CHECKPOINT_SCHEMA_VERSION,
                    ),
                )
            self._checkpoint_count += 1
            self.note_checkpoint_saved()
            logger.debug(
                "Checkpoint %d saved for run %s at step %d",
                self._checkpoint_count,
                self.run_id,
                step_number,
            )
            return True
        except Exception as e:
            logger.warning("Failed to save checkpoint: %s", e)
            return False

    @staticmethod
    def load_latest(run_id: str) -> dict[str, Any] | None:
        """Load the most recent checkpoint for a run. Returns None if not found."""
        try:
            from psycopg2.extras import RealDictCursor

            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute(
                    """
                    SELECT step_number, messages, scratchpad, plan,
                           COALESCE(schema_version, 0) AS schema_version
                    FROM agent_run_checkpoints
                    WHERE run_id = %s
                    ORDER BY step_number DESC
                    LIMIT 1
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                result = dict(row)
                # Skip resume if schema version doesn't match
                saved_version = result.get("schema_version", 0)
                if saved_version != CHECKPOINT_SCHEMA_VERSION:
                    from robothor.engine.sanitize import sanitize_log

                    logger.warning(
                        "Checkpoint schema mismatch for run %s: saved=%d, current=%d — skipping resume",
                        sanitize_log(run_id),
                        saved_version,
                        CHECKPOINT_SCHEMA_VERSION,
                    )
                    return None
                return result
        except Exception as e:
            logger.warning("Failed to load checkpoint: %s", e)
            return None
