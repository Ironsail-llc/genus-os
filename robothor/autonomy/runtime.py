"""Engine-to-broker process boundary. Only references enter tool transcripts."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from robothor.autonomy.broker import ExecutionPlan
    from robothor.autonomy.models import Scope


async def run_browser(
    scope: Scope,
    operation_id: str,
    agent_id: str,
    plan: ExecutionPlan | None,
    *,
    inspect_url: str | None = None,
    session_resource_id: str | None = None,
    managed: bool = False,
    reconcile: bool = False,
    verification_code: str | None = None,
) -> dict[str, Any]:
    store = AutonomyStore()
    settings = await asyncio.to_thread(store.settings, scope)
    if not settings.enabled and not reconcile:
        return {"error": "autonomous_execution_not_enabled", "setup_path": "/account/autonomy"}
    row = await asyncio.to_thread(store.operation, scope, operation_id)
    if row["agent_id"] != agent_id:
        raise PermissionError("agent_not_allowed")
    if (
        row["proposal"]["action"] in {"purchase", "subscription"}
        and not settings.payment_processing
        and not reconcile
    ):
        return {"error": "payment_processing_not_enabled", "setup_path": "/account/autonomy"}
    if managed and not settings.managed_browser:
        return {"error": "managed_browser_not_enabled"}
    if not reconcile:
        await asyncio.to_thread(store.check_authority, scope, operation_id, agent_id)
    if row.get("workflow_id") and not reconcile:
        # Only the authenticated secure-input route supplies a transient code.
        # Attached operations must never reopen a one-shot browser.
        if verification_code is None or plan is None:
            return {"error": "workflow_required", "workflow_id": row["workflow_id"]}
        from uuid import uuid4

        from robothor.autonomy.workflows.client import invoke
        from robothor.autonomy.workflows.store import WorkflowStore

        pending = await asyncio.to_thread(
            WorkflowStore(store).code_resume, scope, agent_id, row["workflow_id"]
        )
        return await invoke(
            scope,
            agent_id,
            {
                "kind": "execute",
                "workflow_id": row["workflow_id"],
                "command_id": str(uuid4()),
                "revision": pending["revision"],
                "advance": pending["advance"],
                "plan": plan.model_dump(mode="json"),
                "verification_code": verification_code,
            },
        )
    from robothor import vault
    from robothor.config import get_config

    key_id, keys = await asyncio.to_thread(store.resource_keyring)

    payload = {
        "scope": scope.model_dump(),
        "operation_id": operation_id,
        "agent_id": agent_id,
        "plan": plan.model_dump(mode="json") if plan else None,
        "inspect_url": inspect_url,
        "session_resource_id": session_resource_id,
        "database": get_config().db.dict,
        "keys": {name: base64.b64encode(value).decode() for name, value in keys.items()},
        "key_id": key_id,
        "managed": managed,
        "reconcile": reconcile,
        "verification_code": verification_code,
        "browserbase_key": await asyncio.to_thread(
            vault.get, "providers/browserbase/api_key", tenant_id=scope.tenant_id
        )
        if managed
        else None,
    }
    if managed and not payload["browserbase_key"]:
        return {"error": "browserbase_not_configured"}
    # No inherited provider tokens, tracing toggles or debugging configuration.
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "TMPDIR",
            "PLAYWRIGHT_BROWSERS_PATH",
            "ROBOTHOR_AUTONOMY_CHROMIUM_EXECUTABLE",
        )
        if key in os.environ
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "robothor.autonomy.worker",
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode()), timeout=180
        )
        try:
            if proc.returncode or len(output) > 100_000:
                raise ValueError
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            current = await asyncio.to_thread(store.operation, scope, operation_id)
            return {
                "operation_id": operation_id,
                "state": current["state"],
                "reason": "broker_unavailable",
            }
        if (
            not managed
            and not reconcile
            and settings.managed_browser
            and (
                result.get("reason") == "preflight_failed"
                or result.get("error") == "broker_request_failed"
            )
        ):
            current = await asyncio.to_thread(store.operation, scope, operation_id)
            if current["state"] == "reserved":
                return await run_browser(
                    scope,
                    operation_id,
                    agent_id,
                    plan,
                    inspect_url=inspect_url,
                    session_resource_id=session_resource_id,
                    managed=True,
                    verification_code=verification_code,
                )
        return result
    except (TimeoutError, asyncio.CancelledError) as exc:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        # The durable marker, not the subprocess exception, determines whether
        # submission could have occurred.
        current = await asyncio.to_thread(store.operation, scope, operation_id)
        return {
            "operation_id": operation_id,
            "state": current["state"],
            "reason": "broker_interrupted",
        }
    finally:
        payload.clear()
