"""Authenticated owner execution outside the shared engine's mount namespace.

The main agent uses the existing exec tool. Workers never mint a host token.
The host service runs as the instance's OS account, with that account's normal
maintenance permissions, rather than making the shared engine privileged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shlex
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request  # noqa: TC002 — FastAPI resolves this at runtime

AUDIENCE = "genus-host-execution"
DEFAULT_SOCKET = "/run/robothor-host/exec.sock"


def socket_path() -> str:
    return os.environ.get("ROBOTHOR_HOST_EXEC_SOCKET", "")


def eligible(ctx: Any) -> bool:
    from robothor.engine.session_registry import lookup

    identity = getattr(ctx, "identity", None)
    if (
        ctx.agent_id != "main"
        or ctx.is_benchmark
        or not identity
        or not identity.verified
        or identity.role != "owner"
        or identity.tenant_id != ctx.tenant_id
    ):
        return False
    session = lookup(ctx.run_id)
    return bool(
        session
        and not session.run.parent_run_id
        and not getattr(session, "readonly_mode", False)
        and "exec" in session.run.tools_provided
    )


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _token(ctx: Any, body: dict[str, Any]) -> str:
    from robothor.auth.tokens import issue_service_token

    return issue_service_token(
        str(ctx.user_id),
        ctx.tenant_id,
        agent_id="main",
        audience=AUDIENCE,
        scopes=("host:exec", _digest(body)),
        ttl_seconds=30,
    )


async def execute(ctx: Any, command: str, timeout: int, env: dict[str, str]) -> dict[str, Any]:
    if not eligible(ctx):
        return {
            "error": "Host execution requires a verified owner main-agent run.",
            "error_type": "host_authority_denied",
        }
    from robothor.engine.session_registry import lookup
    from robothor.engine.task_context import read_context

    session = lookup(ctx.run_id)
    body = {
        "command": command,
        "cwd": ctx.workspace,
        "timeout": timeout,
        "env": env,
        "run_id": ctx.run_id,
        "task_context": read_context(session.messages) if session else None,
        "identity": asdict(ctx.identity),
    }

    transport = httpx.AsyncHTTPTransport(uds=socket_path())
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout + 10) as client:
            response = await client.post(
                "http://host/exec",
                json=body,
                headers={"Authorization": "Bearer " + _token(ctx, body)},
            )
            response.raise_for_status()
            return dict(response.json())
    except asyncio.CancelledError:
        # A disconnected client is also detected by the service. Explicit
        # cancellation shortens the window and kills the whole process group.
        raise
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "error": f"Host execution unavailable: {type(exc).__name__}",
            "error_type": "host_execution_unavailable",
            "execution_mode": "host_service",
        }


async def health() -> dict[str, Any]:
    path = socket_path()
    if not path:
        return {"mode": "engine_process", "available": False, "configured": False}
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=path), timeout=2
        ) as client:
            response = await client.get("http://host/health")
            response.raise_for_status()
            return dict(response.json())
    except (httpx.HTTPError, ValueError):
        return {"mode": "host_service", "available": False, "configured": True}


def create_app() -> Any:
    from fastapi import FastAPI, HTTPException

    from robothor.auth.tokens import TokenError, decode_token
    from robothor.engine.exec_env import build_exec_env

    app = FastAPI()
    used_tokens: dict[str, float] = {}
    slots = asyncio.Semaphore(4)

    @app.get("/health")
    async def ready() -> dict[str, Any]:
        return {
            "mode": "host_service",
            "available": True,
            "configured": True,
            "uid": os.getuid(),
            "loaded_module": str(Path(__file__).resolve()),
            "maintenance": True,
        }

    @app.post("/exec")
    async def run(request: Request) -> dict[str, Any]:
        body = await request.json()
        try:
            claims = decode_token(
                request.headers.get("authorization", "").removeprefix("Bearer "),
                expected_audience=AUDIENCE,
            )
        except TokenError as exc:
            raise HTTPException(403, "invalid host execution token") from exc
        scopes = claims.get("scope", [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        if (
            claims.get("typ") != "service"
            or claims.get("agent_id") != "main"
            or "host:exec" not in scopes
            or _digest(body) not in scopes
        ):
            raise HTTPException(403, "request is not authorized")
        now = time.time()
        for key in list(used_tokens):
            if used_tokens[key] < now:
                del used_tokens[key]
        if claims["jti"] in used_tokens:
            raise HTTPException(409, "request already consumed")
        used_tokens[claims["jti"]] = claims["exp"]
        command = body.get("command")
        if not isinstance(command, str) or not command or len(command) > 100_000:
            raise HTTPException(422, "invalid command")
        if command.startswith("genus-host "):
            parts = shlex.split(command)
            from robothor.engine import local_deploy

            if len(parts) == 3 and parts[1] == "deploy":
                return await asyncio.to_thread(
                    local_deploy.queue,
                    body["cwd"],
                    parts[2],
                    {
                        "run_id": body["run_id"],
                        "task_context": body.get("task_context"),
                        "user_id": claims["sub"],
                        "tenant_id": claims["tid"],
                        "identity": body.get("identity"),
                    },
                )
            if len(parts) == 3 and parts[1] == "status":
                import uuid

                job_id = str(uuid.UUID(parts[2]))
                # The request body is covered by the one-shot service-token
                # digest above; this is an owner-authorized status read.
                # codeql[py/path-injection]
                path = Path(body["cwd"]) / "local/repairs" / job_id / "state.json"
                return dict(json.loads(path.read_text()))
            raise HTTPException(422, "Use genus-host deploy REVISION or genus-host status JOB_ID")
        timeout = max(1, min(int(body.get("timeout", 30)), 900))
        # Never inherit the host service's own bootstrap credentials. The
        # engine supplies the invoking agent's selected grants explicitly.
        env = build_exec_env(agent_id="main", mode="enforce", base=dict(os.environ), grants=()).env
        env.update({str(k): str(v) for k, v in body.get("env", {}).items()})
        import logging

        logging.getLogger(__name__).info("authenticated host exec request accepted")
        async with slots:
            # The command is supplied in the signed, single-use request body;
            # only a verified owner main-agent run can produce that token.
            # codeql[py/command-line-injection]
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=body.get("cwd") or None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            communication = asyncio.create_task(proc.communicate())
            deadline = time.monotonic() + timeout
            error = ""
            try:
                while not communication.done():
                    if time.monotonic() >= deadline:
                        error = "Command timed out"
                        break
                    if await request.is_disconnected():
                        error = "Command cancelled: caller disconnected"
                        break
                    await asyncio.wait({communication}, timeout=0.2)
            finally:
                # Kill descendants even if the shell exited after backgrounding.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = await communication
            result = {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "exit_code": proc.returncode,
                "execution_mode": "host_service",
            }
            if error:
                result.update(error=error, timeout_seconds=timeout)
            return result

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        uds=os.environ.get("ROBOTHOR_HOST_EXEC_SOCKET", DEFAULT_SOCKET),
        log_level="info",
    )


if __name__ == "__main__":
    main()
