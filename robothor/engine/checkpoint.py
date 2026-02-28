"""
Checkpointing — mid-run state persistence for long-running agents.

Saves conversation state periodically to the agent_run_checkpoints table.
Supports resume: reload messages from the latest checkpoint and continue
the conversation loop where it left off.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_INTERVAL = 5  # checkpoint every N successful tool calls


@dataclass
class CheckpointManager:
    """Manages mid-run state snapshots."""

    run_id: str = ""
    interval: int = CHECKPOINT_INTERVAL
    _success_count: int = 0
    _checkpoint_count: int = 0

    def record_success(self) -> None:
        """Record a successful tool call."""
        self._success_count += 1

    def should_checkpoint(self) -> bool:
        """Whether it's time to save a checkpoint."""
        if self._success_count == 0:
            return False
        return self._success_count % self.interval == 0

    def save(
        self,
        step_number: int,
        messages: list[dict[str, Any]],
        scratchpad: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a checkpoint to the database. Best-effort — never raises."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO agent_run_checkpoints
                        (run_id, step_number, messages, scratchpad, plan)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        self.run_id,
                        step_number,
                        json.dumps(messages, default=str),
                        json.dumps(scratchpad, default=str) if scratchpad else None,
                        json.dumps(plan, default=str) if plan else None,
                    ),
                )
            self._checkpoint_count += 1
            logger.debug(
                "Checkpoint %d saved for run %s at step %d",
                self._checkpoint_count, self.run_id, step_number,
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
                    SELECT step_number, messages, scratchpad, plan
                    FROM agent_run_checkpoints
                    WHERE run_id = %s
                    ORDER BY step_number DESC
                    LIMIT 1
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
                return None
        except Exception as e:
            logger.warning("Failed to load checkpoint: %s", e)
            return None
