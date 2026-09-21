"""Engine client; tokens and transient codes never enter model-facing results."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from robothor.auth import tokens
from robothor.autonomy.workflows.protocol import AUDIENCE, RPC, SCOPE, ExecuteRequest
from robothor.settings import get_settings

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope


def _invalid_request(exc: ValidationError) -> dict[str, str]:
    result = {"error": "invalid_workflow_request"}
    # Neither submitted values nor arbitrary field names/messages may escape.
    identifiers = {
        "operation_id": "invalid_operation_id",
        "workflow_id": "invalid_workflow_id",
        "command_id": "invalid_command_id",
    }
    for error in exc.errors(include_input=False, include_url=False):
        location = error["loc"]
        if (
            len(location) == 2
            and location[1] in identifiers
            and error["type"] in {"uuid_parsing", "uuid_type", "missing"}
        ):
            result["reason"] = identifiers[str(location[1])]
            break
        if (
            location == ("execute", "plan")
            and error["type"] == "value_error"
            and str(error.get("ctx", {}).get("error"))
            == "confirmation_selector_and_text_required_together"
        ):
            result["reason"] = "confirmation_selector_and_text_required_together"
            break
    return result


async def invoke(
    scope: Scope,
    agent_id: str,
    request: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    try:
        command = RPC.validate_python(request)
    except ValidationError as exc:
        return _invalid_request(exc)
    payload = command.model_dump(mode="json")
    if isinstance(command, ExecuteRequest) and command.verification_code:
        payload["verification_code"] = command.verification_code.get_secret_value()
    bearer = await asyncio.to_thread(
        tokens.issue_service_token,
        scope.owner_id,
        scope.tenant_id,
        agent_id=agent_id,
        audience=AUDIENCE,
        scopes=(SCOPE,),
        ttl_seconds=60,
    )
    actual_transport = transport or httpx.AsyncHTTPTransport(uds=get_settings().autonomy.socket)
    try:
        async with httpx.AsyncClient(
            transport=actual_transport,
            base_url="http://autonomy",
            timeout=185,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/rpc", json=payload, headers={"Authorization": "Bearer " + bearer}
            )
            if len(response.content) > 100_000:
                return {"error": "workflow_response_too_large"}
            result = response.json()
            return result if isinstance(result, dict) else {"error": "workflow_response_invalid"}
    except (httpx.HTTPError, ValueError):
        return {
            "error": "workflow_broker_unavailable",
            "reason": "Reconnect with the same command_id; do not repeat the action with a new ID.",
        }
    finally:
        payload.clear()
