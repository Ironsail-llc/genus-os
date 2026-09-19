"""Broker networking and managed provider settings without live credentials."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.autonomy import worker
from robothor.autonomy.models import RuntimeSettings


@pytest.mark.parametrize(
    "address,allowed",
    [("127.0.0.1", False), ("169.254.169.254", False), ("::1", False), ("93.184.216.34", True)],
)
async def test_private_destinations_rejected(monkeypatch, address, allowed):
    route = SimpleNamespace(
        request=SimpleNamespace(url="https://shop.example/"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    loop = worker.asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(0, 0, 0, "", (address, 443))])
    )
    await worker.public_request(route)
    assert route.continue_.await_count == int(allowed)
    assert route.abort.await_count == int(not allowed)


async def test_managed_provider_disables_recording_and_enables_challenge_support(monkeypatch):
    store = MagicMock()
    store.settings.return_value = RuntimeSettings(enabled=True, managed_browser=True)
    store.operation.return_value = {
        "agent_id": "main",
        "proposal": {"action": "account", "origin": "https://shop.example"},
    }
    store.check_authority.return_value = SimpleNamespace(frame_origins=frozenset())
    store.put_resource.return_value = {"id": "session-ref"}
    monkeypatch.setattr(worker, "AutonomyStore", lambda *args, **kwargs: store)
    monkeypatch.setattr(
        "robothor.autonomy.inspection.inspect_page",
        AsyncMock(return_value={"fields": [], "terms": [], "frames": []}),
    )
    page = MagicMock(url="https://shop.example/")
    page.goto = AsyncMock()
    page.locator.return_value.evaluate_all = AsyncMock(return_value=[])
    context = SimpleNamespace(
        route=AsyncMock(),
        new_page=AsyncMock(return_value=page),
        storage_state=AsyncMock(return_value={"cookies": [], "origins": []}),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=pw)
    manager.__aexit__ = AsyncMock()
    monkeypatch.setattr(worker, "async_playwright", lambda: manager)
    response = MagicMock()
    response.json.return_value = {"connectUrl": "wss://provider.example/session"}
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=client)
    http.__aexit__ = AsyncMock()
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: http)
    result = await worker.handle(
        {
            "scope": {"tenant_id": "test", "owner_id": "alice"},
            "operation_id": "op",
            "agent_id": "main",
            "database": {},
            "keys": {"v1": base64.b64encode(b"x" * 32).decode()},
            "key_id": "v1",
            "inspect_url": "https://shop.example/",
            "managed": True,
            "browserbase_key": "test-key",
        }
    )
    assert result["session_resource_id"] == "session-ref"
    assert client.post.call_args.kwargs["json"]["browserSettings"] == {
        "recordSession": False,
        "logSession": False,
        "solveCaptchas": True,
    }
    browser.close.assert_awaited_once()


async def test_local_browser_uses_system_chromium_with_sandbox_enabled(monkeypatch):
    monkeypatch.setenv("PRIVATE_PROVIDER_SECRET", "must-not-inherit")
    monkeypatch.setenv("DEBUG", "pw:*")
    monkeypatch.delenv("ROBOTHOR_AUTONOMY_CHROMIUM_EXECUTABLE", raising=False)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/chromium")
    chromium = SimpleNamespace(launch=AsyncMock(return_value="browser"))
    assert await worker.launch_local(chromium) == "browser"
    kwargs = chromium.launch.call_args.kwargs
    assert kwargs["headless"] and kwargs["chromium_sandbox"]
    assert kwargs["executable_path"] == "/usr/bin/chromium"
    assert "PRIVATE_PROVIDER_SECRET" not in kwargs["env"]
    assert "DEBUG" not in kwargs["env"]
