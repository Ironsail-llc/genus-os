"""
Checkpointing — mid-run state persistence for long-running agents.

Saves conversation state periodically to the agent_run_checkpoints table.
Supports resume: reload messages from the latest checkpoint and continue
the conversation loop where it left off.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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


#: What an image block becomes in a checkpoint row. The picture is base64 of
#: up to megabytes and a checkpoint is written many times per run; the turn's
#: text already names the file's path, which is what a resumed run needs.
IMAGE_PLACEHOLDER = "[image omitted from the checkpoint; its path is named in the turn]"


def _redacted(messages: list[Any], stored: tuple[str, ...] = ()) -> list[Any]:
    """*messages* with the history redaction (``redact_message``) applied.

    ``stored`` is a per-restore set of values the vault holds (see
    :func:`_stored_credentials`), replaced here whatever the process-wide
    registry knows. What the person typed (``user``) and the system turn that
    carries the task record also lose every opaque run
    (:func:`redact_opaque_runs`): a bare 32-hex key has no shape. Tool results
    keep theirs: a git SHA there is information.
    """
    from robothor.secrets.redaction import PLACEHOLDER, redact_message, redact_opaque_runs

    out: list[Any] = []
    for message in messages:
        if stored:
            message = _replace_values(message, stored, PLACEHOLDER)
        cleaned = redact_message(message)
        if (
            isinstance(cleaned, dict)
            and cleaned.get("role") in ("user", "system")
            and isinstance(cleaned.get("content"), str)
        ):
            cleaned = {**cleaned, "content": redact_opaque_runs(cleaned["content"])}
        out.append(cleaned)
    return out


def _replace_values(value: Any, stored: tuple[str, ...], placeholder: str) -> Any:
    """A copy of *value* with every string's ``stored`` values replaced."""
    if isinstance(value, str):
        for secret in stored:
            if secret in value:
                value = value.replace(secret, placeholder)
        return value
    if isinstance(value, dict):
        return {k: _replace_values(v, stored, placeholder) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_values(v, stored, placeholder) for v in value]
    return value


#: A UUID, a number, an email address or a URL: an identifier the vault may
#: hold (a tenant or client id, a chat id), never a credential to scrub.
_IDENTIFIER = re.compile(
    r"^(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    r"|[-+]?\d+|[^@\s]+@[^@\s]+\.\w+|\w+://\S+)$"
)


def _is_credential_like(value: str) -> bool:
    """At least 16 characters, two of lower case, upper case and digits, and
    not an identifier (:data:`_IDENTIFIER`)."""
    if len(value) < 16 or _IDENTIFIER.match(value):
        return False
    return sum(any(t(c) for c in value) for t in (str.islower, str.isupper, str.isdigit)) >= 2


def _stored_credentials(tenant_id: str | None) -> tuple[str, ...]:
    """The credential-like values the vault stores, longest first.

    Read in-process through the vault and never logged. Each is also
    registered with the process-wide redactor, so later tool results and
    history redact it after a restart; identifiers the vault holds are left
    out of both. A vault that cannot be read gives none, and the restore still
    redacts by shape and by opaque run.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.secrets.fingerprint import fingerprint
    from robothor.secrets.redaction import register_known_value

    try:
        import robothor.vault as vault

        values = vault.export_env(tenant_id=tenant_id or DEFAULT_TENANT).values()
    except Exception as exc:  # noqa: BLE001 - the restore still redacts by shape
        logger.warning("checkpoint restore: vault unreadable (%s)", type(exc).__name__)
        return ()
    found = {v for v in values if isinstance(v, str) and _is_credential_like(v)}
    stored = tuple(sorted(found, key=len, reverse=True))
    for value in stored:
        register_known_value(value, fingerprint=fingerprint(value))
    return stored


def _at_rest(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*messages* as a checkpoint may keep them: no image bytes, no credential.

    The person's own turn stays raw in ``session.messages`` so the model can
    read a credential once and store it. A checkpoint outlives that turn and is
    read back on resume, so it gets the history redaction (``redact_message``)
    that every other copy of the conversation gets.
    """
    return _redacted(_without_images(messages))


def _without_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of *messages* with every image block replaced by a short note."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list) or not any(
            isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in content
        ):
            out.append(msg)
            continue
        blocks = [
            {"type": "text", "text": IMAGE_PLACEHOLDER}
            if isinstance(b, dict) and b.get("type") in ("image_url", "image")
            else b
            for b in content
        ]
        out.append({**msg, "content": blocks})
    return out


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
        from dataclasses import asdict

        from robothor.engine.runtime.contracts import StateEnvelope

        scratchpad = dict(scratchpad or {})
        scratchpad["_runtime"] = asdict(StateEnvelope())
        if todo_list is not None:
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
                        json.dumps(_at_rest(messages), default=str),
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
            # The class only: a DB error can quote the row, messages included.
            logger.warning("Failed to save checkpoint: %s", type(e).__name__)
            return False

    @staticmethod
    def load_latest(run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
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
                if tenant_id is not None:
                    cur.execute(
                        "SELECT tenant_id,runtime_context FROM agent_runs WHERE id=%s", (run_id,)
                    )
                    owner = cur.fetchone()
                    if not owner or owner["tenant_id"] != tenant_id:
                        return None
                    result["runtime_context"] = owner.get("runtime_context") or {}
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
                # A row written before checkpoints were redacted still holds
                # the raw turn; resume must not read a credential back into
                # the run (and from it, into ``originating_message``).
                if isinstance(result.get("messages"), list):
                    stored = _stored_credentials(tenant_id)
                    result["messages"] = _redacted(result["messages"], stored)
                from robothor.engine.runtime.contracts import StateEnvelope

                envelope = (result.get("scratchpad") or {}).get("_runtime")
                if envelope is not None:
                    StateEnvelope().require_compatible(StateEnvelope(**envelope))
                return result
        except Exception as e:
            logger.warning("Failed to load checkpoint: %s", type(e).__name__)
            return None


async def save_iteration(
    checkpoint: Any,
    session: Any,
    *,
    agent_config: Any,
    scratchpad: Any,
    plan_result: Any,
    hook_registry: Any,
) -> None:
    """Persist one iteration's state, if this is a checkpoint iteration.

    Lifted out of ``_run_loop``: "what does a resumable run have to carry" is
    this module's question, and nothing else in the loop reads the answer. The
    TodoList goes with it — Phase 5 — because without it the checklist was
    silently dropped on resume.
    """
    if not (checkpoint and checkpoint.should_checkpoint()):
        return
    todo_state: dict[str, Any] | None = None
    if session.todo_list:
        try:
            todo_state = session.todo_list.to_dict()
        except Exception:  # noqa: BLE001 - a checklist is never worth the run
            todo_state = None
    # json.dumps of the whole conversation plus an INSERT: off the loop. The
    # thread gets its own copy of the message list, which the loop may extend.
    await asyncio.to_thread(
        checkpoint.save,
        step_number=session._step_counter,
        messages=list(session.messages),
        scratchpad=scratchpad.to_dict() if scratchpad else None,
        plan=plan_result.raw if plan_result and hasattr(plan_result, "raw") else None,
        todo_list=todo_state,
    )
    if not hook_registry:
        return
    from robothor.engine.hook_registry import HookContext, HookEvent

    with contextlib.suppress(Exception):
        await hook_registry.dispatch(
            HookEvent.CHECKPOINT,
            HookContext(
                event=HookEvent.CHECKPOINT,
                agent_id=agent_config.id,
                run_id=session.run_id,
                metadata={"step_number": session._step_counter},
            ),
        )
