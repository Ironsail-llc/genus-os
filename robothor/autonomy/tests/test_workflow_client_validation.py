"""Malformed commands return useful fixed diagnostics without disclosing inputs."""

from uuid import uuid4

import pytest

from robothor.autonomy.models import Scope
from robothor.autonomy.workflows.client import invoke


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (
            {"kind": "open", "operation_id": "private-canary", "url": "https://shop.example"},
            "invalid_operation_id",
        ),
        ({"kind": "status", "workflow_id": "private-canary"}, "invalid_workflow_id"),
        (
            {
                "kind": "execute",
                "workflow_id": str(uuid4()),
                "command_id": str(uuid4()),
                "revision": 0,
                "plan": {
                    "url": "https://shop.example",
                    "submit_selector": "#submit",
                    "success_selector": "#private-canary",
                },
            },
            "confirmation_selector_and_text_required_together",
        ),
        ({"kind": "private-canary"}, None),
        ({"kind": "status", "workflow_id": str(uuid4()), "private-canary": "secret"}, None),
    ],
)
async def test_invalid_commands_never_issue_tokens_or_echo_values(monkeypatch, command, reason):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached authentication or transport")

    monkeypatch.setattr("robothor.autonomy.workflows.client.tokens.issue_service_token", forbidden)
    result = await invoke(Scope(tenant_id="test", owner_id="owner"), "main", command)
    assert result == {"error": "invalid_workflow_request", **({"reason": reason} if reason else {})}
    assert "private-canary" not in str(result)
