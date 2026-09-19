"""Resume an owner task after its independent deployment job verifies.

Correlation IDs make recovery idempotent across restarts. Any action involving
external state is rechecked by the resumed agent before it retries a mutation.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from robothor.engine.local_deploy import save


def existing_resume(job_id: str) -> str | None:
    from robothor.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agent_runs WHERE correlation_id=%s ORDER BY started_at LIMIT 1",
            (job_id,),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


_resume_lock = asyncio.Lock()


async def resume(job_id: str, runner: Any, config: Any) -> dict[str, Any]:
    async with _resume_lock:
        return await _resume(job_id, runner, config)


async def recover(runner: Any, config: Any) -> None:
    for path in (Path(config.workspace) / "local/repairs").glob("*/state.json"):
        try:
            await resume(path.parent.name, runner, config)
        except Exception:
            logging.getLogger(__name__).exception("Repair recovery failed for %s", path.parent.name)


async def _resume(job_id: str, runner: Any, config: Any) -> dict[str, Any]:
    from robothor.engine.config import load_agent_config
    from robothor.engine.models import DeliveryMode, TriggerType
    from robothor.engine.task_registry import get_task_registry
    from robothor.identity import resolve_identity

    job_id = str(uuid.UUID(job_id))
    path = Path(config.workspace) / "local/repairs" / job_id / "state.json"
    if not path.exists():
        return {"error": "Repair job not found"}
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        job = json.loads(path.read_text())
        continuation = job.get("continuation") or {}
        task = continuation.get("task_context") or {}
        if job.get("status") != "verified" or task.get("mode") != "execute":
            return {"status": "no_execution_continuation"}
        prior = await asyncio.to_thread(existing_resume, job_id)
        if prior:
            return {"status": "already_resumed", "run_id": prior}
        # Persisted identity is attribution, not a new grant: re-resolve it.
        owner = continuation.get("identity") or {}
        identity = await asyncio.to_thread(
            resolve_identity,
            owner.get("channel", ""),
            owner.get("identifier", ""),
            tenant_id=continuation.get("tenant_id", ""),
        )
        if not identity or not identity.verified or identity.role != "owner":
            return {"error": "Original owner identity no longer authorizes execution"}
        if job.get("resume_status") == "scheduled" and job.get("resume_owner_pid") == os.getpid():
            return {"status": "scheduled"}
        agent = load_agent_config("main", config.manifest_dir)
        if agent is None:
            return {"error": "Main agent configuration unavailable"}
        job["resume_status"] = "scheduled"
        job["resume_owner_pid"] = os.getpid()
        save(path, job)
        message = (
            "The repair required by your task is now deployed and independently verified. "
            "Continue the original request below; do not repeat the completed repair or switch "
            "to another historical task. Check current external state before retrying any "
            "booking, payment, message, or other mutation; do not duplicate prior effects. "
            "Ask only for genuinely missing decisions.\n\nOriginal task context:\n"
            + json.dumps(task)
            + "\n\nVerified repair evidence:\n"
            + json.dumps(job.get("evidence"))
        )

        async def continue_task() -> None:
            try:
                run = await runner.execute(
                    agent_id="main",
                    message=message,
                    agent_config=replace(agent, delivery_mode=DeliveryMode.NONE),
                    trigger_type=TriggerType.MANUAL,
                    trigger_detail="repair-resume:" + job_id,
                    correlation_id=job_id,
                    execution_mode=True,
                    tenant_id=identity.tenant_id,
                    user_id=continuation.get("user_id", ""),
                    user_role="owner",
                    identity=identity,
                )
                latest = json.loads(path.read_text())
                latest.update(
                    resume_status="finished",
                    resume_run_id=run.id,
                    resume_output=run.output_text,
                    resume_error=run.error_message,
                )
                save(path, latest)
            except BaseException:
                latest = json.loads(path.read_text())
                latest["resume_status"] = "interrupted"
                save(path, latest)
                raise

        get_task_registry().spawn(continue_task(), name="repair-resume:" + job_id)
        return {"status": "scheduled", "job_id": job_id}
