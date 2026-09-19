from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import browser


def test_browser_key_isolates_tenants_users_and_runs():
    contexts = [
        ToolContext(agent_id="main", tenant_id=t, user_id=u, run_id=r)
        for t, u, r in [
            ("one", "alice", "r1"),
            ("two", "alice", "r1"),
            ("one", "bob", "r1"),
            ("one", "alice", "r2"),
        ]
    ]
    assert len({browser._session_key(ctx) for ctx in contexts}) == 4


async def test_native_upload_reads_only_an_allowed_workspace_file(tmp_path, monkeypatch):
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"image bytes")
    locator = MagicMock()
    locator.set_input_files = AsyncMock()
    page = MagicMock()
    page.locator.return_value.first = locator
    monkeypatch.setattr(browser, "_get_session", AsyncMock(return_value=SimpleNamespace(page=page)))
    ctx = ToolContext(agent_id="main", workspace=str(tmp_path))
    result = await browser._action_act(
        {"request": {"kind": "upload", "selector": "#photo", "path": "photo.png"}}, ctx
    )
    assert result["acted"] == "upload"
    assert locator.set_input_files.await_args.args[0]["buffer"] == b"image bytes"
    (tmp_path / "credentials.json").write_text("private")
    result = await browser._action_act(
        {"request": {"kind": "upload", "selector": "#photo", "path": "credentials.json"}}, ctx
    )
    assert "error" in result
    assert locator.set_input_files.await_count == 1


async def test_upload_cannot_escape_workspace(tmp_path, monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(browser, "_get_session", AsyncMock(return_value=SimpleNamespace(page=page)))
    result = await browser._action_act(
        {"request": {"kind": "upload", "selector": "#photo", "path": "/etc/passwd"}},
        ToolContext(agent_id="main", workspace=str(tmp_path)),
    )
    assert "error" in result


async def test_upload_refuses_quarantined_inbox_attachment(tmp_path, monkeypatch):
    from robothor.engine import attachments

    secret = attachments.inbox_root(tmp_path) / "chat" / "2026-01-01" / "secret" / "opaque.bin"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"private")
    page = MagicMock()
    monkeypatch.setattr(browser, "_get_session", AsyncMock(return_value=SimpleNamespace(page=page)))
    result = await browser._action_act(
        {"request": {"kind": "upload", "selector": "#photo", "path": str(secret)}},
        ToolContext(agent_id="main", workspace=str(tmp_path)),
    )
    assert "error" in result
    page.locator.return_value.first.set_input_files.assert_not_called()
