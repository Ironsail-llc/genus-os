"""Process failure reporting follows the durable journal, never a guess."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.autonomy import runtime
from robothor.autonomy.models import RuntimeSettings, Scope


@pytest.mark.parametrize("exit_code,stdout", [(1, b""), (0, b"invalid json")])
async def test_failed_worker_preserves_actual_reserved_state(monkeypatch, exit_code, stdout):
    from robothor import config

    store = MagicMock()
    store.settings.return_value = RuntimeSettings(enabled=True)
    store.operation.return_value = {
        "agent_id": "main",
        "state": "reserved",
        "proposal": {"action": "account"},
    }
    store.resource_keyring.return_value = ("v1", {"v1": b"x" * 32})
    monkeypatch.setattr(runtime, "AutonomyStore", lambda: store)
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(db=SimpleNamespace(dict={})))
    proc = SimpleNamespace(returncode=exit_code, communicate=AsyncMock(return_value=(stdout, None)))
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    output = await runtime.run_browser(
        Scope(tenant_id="test", owner_id="alice"),
        "op",
        "main",
        None,
        inspect_url="https://shop.example",
    )
    assert output["state"] == "reserved"
    assert output["reason"] == "broker_unavailable"
