"""Authenticated private RPC; validation never echoes submitted values."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from robothor.auth import tokens
from robothor.autonomy.models import Scope
from robothor.autonomy.workflows.protocol import AUDIENCE, RPC, SCOPE, ExecuteRequest, OpenRequest

if TYPE_CHECKING:
    from robothor.autonomy.workflows.manager import WorkflowManager


def create_app(manager: WorkflowManager) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/ready")
    async def ready() -> dict[str, str | int]:
        return {"status": "ok", "protocol": 1}

    @app.post("/rpc")
    async def rpc(request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        try:
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise tokens.TokenError("missing_token")
            claims = await asyncio.to_thread(
                tokens.decode_token, authorization[7:], expected_audience=AUDIENCE
            )
            if (
                claims.get("typ") != "service"
                or SCOPE not in claims.get("scope", "").split()
                or not claims.get("agent_id")
            ):
                raise tokens.TokenError("wrong_token")
            scope = Scope(tenant_id=claims["tid"], owner_id=claims["sub"])
            agent_id = claims["agent_id"]
        except Exception:
            return JSONResponse(
                {"error": "workflow_authentication_required"}, status_code=401, headers=headers
            )
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    return JSONResponse(
                        {"error": "request_too_large"}, status_code=413, headers=headers
                    )
            command = RPC.validate_json(bytes(body))
            body.clear()
        except Exception:
            return JSONResponse(
                {"error": "invalid_workflow_request"}, status_code=422, headers=headers
            )
        try:
            async with asyncio.timeout(175):
                if isinstance(command, OpenRequest):
                    result = await manager.open(
                        scope,
                        agent_id,
                        str(command.operation_id),
                        command.url,
                        session_resource_id=str(command.session_resource_id)
                        if command.session_resource_id
                        else None,
                    )
                elif isinstance(command, ExecuteRequest):
                    result = await manager.execute(
                        scope,
                        agent_id,
                        str(command.workflow_id),
                        str(command.command_id),
                        command.revision,
                        command.plan,
                        advance=command.advance,
                        verification_code=command.verification_code.get_secret_value()
                        if command.verification_code
                        else None,
                    )
                elif command.kind == "status":
                    result = await manager.status(scope, agent_id, str(command.workflow_id))
                elif command.kind == "close":
                    result = await manager.close(scope, agent_id, str(command.workflow_id))
                else:
                    result = await manager.inspect(scope, agent_id, str(command.workflow_id))
            return JSONResponse(result, headers=headers)
        except PermissionError:
            return JSONResponse(
                {"error": "workflow_not_authorized_or_unavailable"},
                status_code=403,
                headers=headers,
            )
        except Exception:
            return JSONResponse(
                {"error": "workflow_request_failed"}, status_code=503, headers=headers
            )

    return app
