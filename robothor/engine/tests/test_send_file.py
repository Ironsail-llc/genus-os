"""An agent that made a file must be able to hand it to the operator.

Until now it could not. There was no tool and no delivery path that sent a
photo or a document to Telegram — the only ``reply_document`` in the tree was a
slash command's markdown export. An agent that produced a chart, a PDF, a
screenshot or a CSV had to paste it as text or describe it.

The refusals here matter as much as the sends. ``send_file`` is a tool that
takes a PATH and puts whatever is at it in front of a person, so it is the
shortest exfiltration route this platform has: a path outside the workspace, a
symlink that leaves it, a secrets file by name, a text file with a live token
in it. Each one has a test, and each one refuses before a byte is read.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime by the helpers below

import pytest

from robothor.engine.channels.base import SendReceipt
from robothor.engine.tools.handlers import attachments as tool


class Ctx:
    def __init__(self, workspace, **kw):
        self.workspace = str(workspace)
        self.agent_id = kw.get("agent_id", "main")
        self.run_id = kw.get("run_id", "run-1")
        self.tenant_id = kw.get("tenant_id", "t-alpha")
        self.user_id = kw.get("user_id", "tu-1")
        self.user_role = kw.get("user_role", "owner")
        self.identity = kw.get("identity")


def _route(channel: str, target: str):
    """A stand-in for the async run-lookup that resolves this run's surface."""

    async def _resolve(ctx):
        return channel, target

    return _resolve


@pytest.fixture
def sent(monkeypatch):
    """Record what reaches the channel, and never reach Telegram."""
    calls: list[dict] = []

    class FakeChannel:
        name = "telegram"

        async def send_attachment(self, target, path, caption="", *, as_="auto", **kw):
            calls.append({"target": target, "path": str(path), "caption": caption, "as_": as_})
            return SendReceipt(acknowledged=1, expected=1, platform_ids=["4242"], target=target)

    monkeypatch.setattr(
        "robothor.engine.channels.get_channel", lambda name: FakeChannel(), raising=True
    )
    monkeypatch.setattr(tool, "_originating_route", _route("telegram", "100200300"), raising=True)
    return calls


def make_file(root: Path, name: str, content: bytes = b"hello") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def png(root: Path, name: str = "chart.png", size=(40, 30)) -> Path:
    from PIL import Image

    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 90, 200)).save(path)
    return path


class TestSending:
    @pytest.mark.asyncio
    async def test_a_document_goes_out_with_its_caption(self, tmp_path, sent) -> None:
        path = make_file(tmp_path, "report.pdf", b"%PDF-1.7")
        out = await tool.send_file(
            {"path": str(path), "caption": "here is the quarterly"}, Ctx(tmp_path)
        )
        assert out["sent"] is True
        assert out["as"] == "document"
        assert out["message_id"] == "4242"
        assert sent[0]["caption"] == "here is the quarterly"
        assert sent[0]["target"] == "100200300"

    @pytest.mark.asyncio
    async def test_auto_picks_photo_for_a_small_image(self, tmp_path, sent) -> None:
        out = await tool.send_file({"path": str(png(tmp_path))}, Ctx(tmp_path))
        assert out["as"] == "photo"

    @pytest.mark.asyncio
    async def test_an_oversized_image_goes_as_a_document_not_refused(
        self, tmp_path, sent, monkeypatch
    ) -> None:
        monkeypatch.setattr(tool, "MAX_PHOTO_BYTES", 4, raising=True)
        out = await tool.send_file({"path": str(png(tmp_path))}, Ctx(tmp_path))
        assert out["as"] == "document"
        assert "too large to send as a photo" in (out.get("note") or "")

    @pytest.mark.asyncio
    async def test_a_very_wide_image_goes_as_a_document(self, tmp_path, sent) -> None:
        out = await tool.send_file(
            {"path": str(png(tmp_path, "wide.png", size=(9000, 2000)))}, Ctx(tmp_path)
        )
        assert out["as"] == "document"

    @pytest.mark.asyncio
    async def test_an_explicit_as_document_is_honoured(self, tmp_path, sent) -> None:
        out = await tool.send_file({"path": str(png(tmp_path)), "as": "document"}, Ctx(tmp_path))
        assert out["as"] == "document"

    @pytest.mark.asyncio
    async def test_the_receipt_is_what_the_channel_proved(self, tmp_path, monkeypatch) -> None:
        class SilentChannel:
            name = "telegram"

            async def send_attachment(self, target, path, caption="", *, as_="auto", **kw):
                return SendReceipt(acknowledged=0, expected=1, target=target)

        monkeypatch.setattr(
            "robothor.engine.channels.get_channel", lambda name: SilentChannel(), raising=True
        )
        monkeypatch.setattr(
            tool, "_originating_route", _route("telegram", "100200300"), raising=True
        )
        out = await tool.send_file({"path": str(make_file(tmp_path, "a.txt"))}, Ctx(tmp_path))
        assert out["sent"] is False
        assert "error" in out


class TestContainment:
    @pytest.mark.asyncio
    async def test_a_path_outside_the_workspace_is_refused(self, tmp_path, sent) -> None:
        outside = tmp_path.parent / "elsewhere.txt"
        outside.write_text("not yours")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        out = await tool.send_file({"path": str(outside)}, Ctx(workspace))
        assert "error" in out
        assert not sent

    @pytest.mark.asyncio
    async def test_a_symlink_that_leaves_the_workspace_is_refused(self, tmp_path, sent) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        secret = tmp_path / "outside" / "robothor"
        secret.parent.mkdir(parents=True)
        secret.write_text("pretend this is /etc/robothor")
        link = workspace / "shortcut"
        link.symlink_to(secret)
        out = await tool.send_file({"path": str(link)}, Ctx(workspace))
        assert "error" in out, "the symlink target decides, not the link's own path"
        assert not sent

    @pytest.mark.asyncio
    async def test_traversal_in_the_argument_is_refused(self, tmp_path, sent) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        out = await tool.send_file({"path": "../../../../etc/passwd"}, Ctx(workspace))
        assert "error" in out
        assert not sent

    @pytest.mark.asyncio
    async def test_the_inbox_counts_as_inside(self, tmp_path, sent) -> None:
        """A file the operator just sent must be sendable back."""
        received = make_file(tmp_path / "inbox" / "telegram" / "100200300", "u-a.txt")
        out = await tool.send_file({"path": str(received)}, Ctx(tmp_path))
        assert out.get("sent") is True

    @pytest.mark.asyncio
    async def test_a_directory_is_refused(self, tmp_path, sent) -> None:
        (tmp_path / "folder").mkdir()
        out = await tool.send_file({"path": str(tmp_path / "folder")}, Ctx(tmp_path))
        assert "error" in out
        assert not sent


class TestSecrets:
    @pytest.mark.asyncio
    async def test_a_secrets_file_by_name_is_refused(self, tmp_path, sent) -> None:
        path = make_file(tmp_path, ".env", b"TOKEN=abc")
        out = await tool.send_file({"path": str(path)}, Ctx(tmp_path))
        assert "error" in out
        assert "secrets file" in out["error"]
        assert not sent

    @pytest.mark.asyncio
    async def test_an_ssh_key_is_refused(self, tmp_path, sent) -> None:
        path = make_file(tmp_path, ".ssh/id_rsa", b"-----BEGIN OPENSSH PRIVATE KEY-----")
        out = await tool.send_file({"path": str(path)}, Ctx(tmp_path))
        assert "error" in out
        assert not sent

    @pytest.mark.asyncio
    async def test_a_text_file_carrying_a_token_is_refused(self, tmp_path, sent) -> None:
        path = make_file(
            tmp_path,
            "notes.md",
            b"deploy with\n\nGITHUB_TOKEN: ghp_0123456789abcdefghijklmnopqrstuvwxyz\n",
        )
        out = await tool.send_file({"path": str(path)}, Ctx(tmp_path))
        assert "error" in out
        assert "credential" in out["error"].lower()
        assert not sent

    @pytest.mark.asyncio
    async def test_the_refusal_never_quotes_the_credential(self, tmp_path, sent) -> None:
        path = make_file(tmp_path, "notes.md", b"password: hunter2hunter2hunter2\n")
        out = await tool.send_file({"path": str(path)}, Ctx(tmp_path))
        assert "hunter2" not in out.get("error", "")

    @pytest.mark.asyncio
    async def test_a_binary_file_is_not_scanned_as_text(self, tmp_path, sent) -> None:
        """The scan is for text an agent could have written a secret into. A PNG
        is not text, and decoding one as UTF-8 to grep it would refuse at random."""
        out = await tool.send_file({"path": str(png(tmp_path))}, Ctx(tmp_path))
        assert out.get("sent") is True


class TestLimits:
    @pytest.mark.asyncio
    async def test_a_file_over_the_document_limit_is_refused(
        self, tmp_path, sent, monkeypatch
    ) -> None:
        monkeypatch.setattr(tool, "MAX_DOCUMENT_BYTES", 8, raising=True)
        path = make_file(tmp_path, "big.bin", b"x" * 100)
        out = await tool.send_file({"path": str(path)}, Ctx(tmp_path))
        assert "error" in out
        assert "50 MB" in out["error"] or "8 B" in out["error"]
        assert not sent

    @pytest.mark.asyncio
    async def test_an_empty_file_is_refused_before_the_upload(self, tmp_path, sent) -> None:
        out = await tool.send_file({"path": str(make_file(tmp_path, "z.txt", b""))}, Ctx(tmp_path))
        assert "error" in out
        assert not sent

    @pytest.mark.asyncio
    async def test_a_missing_file_says_so(self, tmp_path, sent) -> None:
        out = await tool.send_file({"path": str(tmp_path / "nope.txt")}, Ctx(tmp_path))
        assert "error" in out
        assert not sent


class TestTargets:
    @pytest.mark.asyncio
    async def test_the_default_target_is_the_run_s_own_chat(self, tmp_path, sent) -> None:
        await tool.send_file({"path": str(make_file(tmp_path, "a.txt"))}, Ctx(tmp_path))
        assert sent[0]["target"] == "100200300"

    @pytest.mark.asyncio
    async def test_a_non_operator_may_not_redirect_the_file(self, tmp_path, sent) -> None:
        out = await tool.send_file(
            {"path": str(make_file(tmp_path, "a.txt")), "target": "999999"},
            Ctx(tmp_path, user_role="member"),
        )
        assert "error" in out
        assert not sent

    @pytest.mark.asyncio
    async def test_an_operator_may(self, tmp_path, sent) -> None:
        out = await tool.send_file(
            {"path": str(make_file(tmp_path, "a.txt")), "target": "999999"},
            Ctx(tmp_path, user_role="owner"),
        )
        assert out.get("sent") is True
        assert sent[0]["target"] == "999999"

    @pytest.mark.asyncio
    async def test_a_call_with_no_surface_and_no_run_is_told_so(
        self, tmp_path, monkeypatch
    ) -> None:
        """No chat to reply to and no run whose report it could ride on. A
        scheduled run takes the queueing branch instead — see
        ``test_delivery_attachments.py``."""
        monkeypatch.setattr(tool, "_originating_route", _route("", ""), raising=True)
        out = await tool.send_file(
            {"path": str(make_file(tmp_path, "a.txt"))}, Ctx(tmp_path, run_id="")
        )
        assert "error" in out
        assert "queued" not in out


class TestAudit:
    @pytest.mark.asyncio
    async def test_the_audit_names_the_file_and_never_its_contents(
        self, tmp_path, sent, monkeypatch
    ) -> None:
        events: list[dict] = []
        monkeypatch.setattr(
            "robothor.audit.logger.log_event",
            lambda **kw: events.append(kw),
            raising=True,
        )
        path = make_file(tmp_path, "report.pdf", b"%PDF-secret-contents")
        await tool.send_file({"path": str(path), "caption": "here"}, Ctx(tmp_path))
        assert events, "a file leaving the box is an audited event"
        details = events[0]["details"]
        assert details["file"] == "report.pdf"
        assert details["size"] == len(b"%PDF-secret-contents")
        assert details["kind"] == "document"
        assert details["target"] == "100200300"
        blob = repr(events[0])
        assert "secret-contents" not in blob
        assert str(path.parent) not in blob, "the basename, never the whole path"


class TestSchema:
    def test_the_tool_is_registered_and_described(self) -> None:
        from robothor.engine.tools.dispatch import builtin_handlers
        from robothor.engine.tools.schemas import get_engine_schemas

        assert "send_file" in builtin_handlers()
        schema = get_engine_schemas()["send_file"]["function"]
        assert set(schema["parameters"]["properties"]) == {"path", "caption", "as", "target"}
        assert schema["parameters"]["required"] == ["path"]

    def test_it_is_not_a_plan_mode_tool(self) -> None:
        """Sending a file to a person is a side effect."""
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert "send_file" not in READONLY_TOOLS
