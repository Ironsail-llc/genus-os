"""``gws_gmail_get_attachment``: download, keep, extract — on either mail provider.

The REAL handler, through ``gws.HANDLERS`` with a ToolContext: Gmail behind a
scripted gws CLI, Microsoft 365 behind the fake Exchange tenant. The file lands
in the channel inbox (``<workspace>/inbox/email/<provider>/<day>/``) through
the same ``attachments.save_attachment`` Telegram files use, so the inbox's
secret quarantine and retention apply to it unchanged.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from robothor.engine import attachments
from robothor.engine.tests.test_file_extract import make_pdf_bytes, make_xlsx
from robothor.engine.tools import constants as c
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_mail import FakeExchangeMail, install_mail

TOOL = c.MAIL_ATTACHMENT_TOOL
ME = "assistant@example.com"
BOB = "bob@example.com"
TENANT = "tenant-a"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _ctx(workspace: Path, *, benchmark: bool = False) -> ToolContext:
    return ToolContext(
        agent_id="main",
        run_id="run-1",
        tenant_id=TENANT,
        workspace=str(workspace),
        is_benchmark=benchmark,
    )


async def _call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await gws.HANDLERS[TOOL](args, ctx)


# ── Gmail ────────────────────────────────────────────────────────────


class Gmail:
    """A scripted gws CLI holding one message with the given attachment parts."""

    def __init__(self) -> None:
        self.parts: list[dict[str, Any]] = []
        self.blobs: dict[str, bytes] = {}
        self.calls: list[list[str]] = []

    def attach(self, part_id: str, name: str, mime: str, data: bytes, *, size: int | None = None):
        aid = f"ANGjdJ-{part_id}-opaque"
        self.blobs[aid] = data
        self.parts.append(
            {
                "partId": part_id,
                "mimeType": mime,
                "filename": name,
                "body": {"attachmentId": aid, "size": len(data) if size is None else size},
            }
        )

    def __call__(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        self.calls.append(list(args))
        verb = " ".join(args[:5])
        params = json.loads(args[args.index("--params") + 1])
        if verb == "gmail users messages get --params":
            return {
                "id": params["id"],
                "threadId": "t-1",
                "payload": {"mimeType": "multipart/mixed", "headers": [], "parts": self.parts},
            }
        if verb == "gmail users messages attachments get":
            data = self.blobs[params["id"]]
            return {"size": len(data), "data": _b64url(data)}
        raise AssertionError(f"unscripted gws call: {args}")


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> Gmail:
    from robothor import workspace
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_WORKSPACE_PROVIDER", raising=False)
    reset_settings()
    workspace.reset_workspace_cache()
    fake = Gmail()
    monkeypatch.setattr(gws, "_run_gws", fake)
    yield fake
    workspace.reset_workspace_cache()
    reset_settings()


async def test_gmail_pdf_is_saved_and_extracted(gmail: Gmail, tmp_path: Path) -> None:
    pdf = make_pdf_bytes("Signed contract for Alice")
    gmail.attach("1", "contract.pdf", "application/pdf", pdf)

    out = await _call({"message_id": "msg-1", "attachment_id": "1"}, _ctx(tmp_path))

    assert out["filename"] == "contract.pdf"
    assert out["mime_type"] == "application/pdf"
    assert out["size_bytes"] == len(pdf)
    assert out["format"] == "pdf"
    assert "Signed contract for Alice" in out["text"]
    assert out["truncated"] is False
    path = Path(out["path"])
    assert path.read_bytes() == pdf
    inbox = attachments.inbox_root(tmp_path, channel="email")
    assert inbox in path.parents
    assert path.parent.parent.name == "google"  # <inbox>/email/<provider>/<day>/
    assert oct(path.stat().st_mode & 0o777) == "0o600"


async def test_gmail_xlsx_is_extracted(gmail: Gmail, tmp_path: Path) -> None:
    data = make_xlsx(tmp_path / "src.xlsx").read_bytes()
    gmail.attach(
        "2",
        "revenue.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data,
    )
    out = await _call({"message_id": "msg-1", "attachment_id": "2"}, _ctx(tmp_path / "ws"))
    assert out["format"] == "xlsx"
    assert "Region" in out["text"]
    assert "remember the milk" in out["text"]


async def test_plain_text_attachment_comes_back_as_text(gmail: Gmail, tmp_path: Path) -> None:
    gmail.attach("3", "notes.txt", "text/plain", b"call Bob back")
    out = await _call({"message_id": "msg-1", "attachment_id": "3"}, _ctx(tmp_path))
    assert out["format"] == "text"
    assert out["text"] == "call Bob back"


async def test_an_image_points_at_view_image(gmail: Gmail, tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    gmail.attach("4", "chart.png", "image/png", png)
    out = await _call({"message_id": "msg-1", "attachment_id": "4"}, _ctx(tmp_path))
    assert out["format"] == "image"
    assert "view_image" in out["message"]
    assert "text" not in out
    assert Path(out["path"]).read_bytes() == png


async def test_an_unreadable_binary_is_kept_and_summarised(gmail: Gmail, tmp_path: Path) -> None:
    gmail.attach("5", "blob.bin", "application/octet-stream", b"\x00\x01\x02")
    out = await _call({"message_id": "msg-1", "attachment_id": "5"}, _ctx(tmp_path))
    assert out["format"] == "binary"
    assert "text" not in out
    assert Path(out["path"]).exists()


async def test_a_credentials_file_is_quarantined_not_read(gmail: Gmail, tmp_path: Path) -> None:
    gmail.attach("6", ".env", "text/plain", b"API_KEY=sk-not-a-real-key")
    out = await _call({"message_id": "msg-1", "attachment_id": "6"}, _ctx(tmp_path))
    assert out["secret"] is True
    assert "text" not in out
    assert "sk-not-a-real-key" not in json.dumps(out)
    assert attachments.SECRET_SUBDIR in Path(out["path"]).parts
    # read_file / send_file ask this before they open a path.
    assert attachments.is_inbox_secret(out["path"], workspace=tmp_path)


async def test_the_size_cap_refuses_before_download(gmail: Gmail, tmp_path: Path) -> None:
    gmail.attach("7", "huge.pdf", "application/pdf", b"x", size=attachments.MAX_DOWNLOAD_BYTES + 1)
    out = await _call({"message_id": "msg-1", "attachment_id": "7"}, _ctx(tmp_path))
    assert "error" in out
    assert "20 MB" in out["error"]
    assert not any("attachments" in c for c in gmail.calls)
    assert not (tmp_path / attachments.INBOX_DIRNAME).exists()


async def test_the_size_cap_also_holds_on_the_bytes(
    gmail: Gmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that under-reports the size still cannot land an oversized file."""
    monkeypatch.setattr(attachments, "MAX_DOWNLOAD_BYTES", 10)
    gmail.attach("8", "liar.pdf", "application/pdf", b"y" * 50, size=5)
    out = await _call({"message_id": "msg-1", "attachment_id": "8"}, _ctx(tmp_path))
    assert "error" in out
    assert not (tmp_path / attachments.INBOX_DIRNAME).exists()


async def test_long_text_is_capped_and_says_so(gmail: Gmail, tmp_path: Path) -> None:
    gmail.attach("9", "long.txt", "text/plain", b"a" * (attachments.TEXT_EXTRACT_CHARS + 10))
    out = await _call({"message_id": "msg-1", "attachment_id": "9"}, _ctx(tmp_path))
    assert out["truncated"] is True
    assert len(out["text"]) == attachments.TEXT_EXTRACT_CHARS


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"message_id": "msg-1"},
        {"attachment_id": "1"},
        {"message_id": " ", "attachment_id": "1"},
    ],
)
async def test_missing_ids_are_refused(gmail: Gmail, tmp_path: Path, args) -> None:
    out = await _call(args, _ctx(tmp_path))
    assert out["hint"].startswith("invalid_params")
    assert gmail.calls == []


async def test_the_same_attachment_twice_is_one_file(gmail: Gmail, tmp_path: Path) -> None:
    gmail.attach("1", "a.txt", "text/plain", b"same")
    first = await _call({"message_id": "msg-1", "attachment_id": "1"}, _ctx(tmp_path))
    second = await _call({"message_id": "msg-1", "attachment_id": "1"}, _ctx(tmp_path))
    assert first["path"] == second["path"]


async def test_benchmark_never_reaches_the_mailbox(gmail: Gmail, tmp_path: Path) -> None:
    out = await _call({"message_id": "msg-1", "attachment_id": "1"}, _ctx(tmp_path, benchmark=True))
    assert out["guard"] == "is_benchmark"
    assert gmail.calls == []


# ── Microsoft 365 ────────────────────────────────────────────────────


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("tok")

    async def token(self) -> str:
        return "tok"


@pytest.fixture
def tenant() -> FakeGraphTenant:
    return FakeGraphTenant()


@pytest.fixture
def exchange(tenant: FakeGraphTenant) -> FakeExchangeMail:
    return install_mail(tenant)


@pytest.fixture
async def m365(monkeypatch: pytest.MonkeyPatch, tenant: FakeGraphTenant, exchange):
    from robothor import workspace
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", ME)
    reset_settings()
    workspace.reset_workspace_cache()
    clients: list[GraphClient] = []

    async def from_vault(tenant_id: str, **_: Any) -> GraphClient:
        client = GraphClient(StaticToken(tenant), transport=tenant.transport())
        clients.append(client)
        return client

    monkeypatch.setattr("robothor.workspace.microsoft.graph_client_from_vault", from_vault)

    def no_cli(*a: Any, **k: Any) -> Any:
        raise AssertionError("a mail call reached the gws CLI under microsoft365")

    monkeypatch.setattr(gws, "_run_gws", no_cli)
    yield
    for client in clients:
        await client.aclose()
    workspace.reset_workspace_cache()
    reset_settings()


def _file(aid: str, name: str, data: bytes, mime: str) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": aid,
        "name": name,
        "contentType": mime,
        "size": len(data),
        "isInline": False,
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }


async def test_outlook_listing_then_download(m365, exchange, tmp_path: Path) -> None:
    data = make_xlsx(tmp_path / "src.xlsx").read_bytes()
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    msg = exchange.deliver(
        ME, sender=BOB, to=[ME], attachments=[_file("AAMk+x/1=", "revenue.xlsx", data, xlsx)]
    )
    listed = await gws.HANDLERS[c.MAIL_GET_TOOL]({"message_id": msg["id"]}, _ctx(tmp_path))
    (entry,) = listed["attachments"]

    out = await _call(
        {"message_id": msg["id"], "attachment_id": entry["attachment_id"]}, _ctx(tmp_path / "ws")
    )

    assert out["filename"] == "revenue.xlsx"
    assert out["format"] == "xlsx"
    assert "remember the milk" in out["text"]
    path = Path(out["path"])
    assert path.read_bytes() == data
    assert path.parent.parent.name == "microsoft365"


async def test_outlook_attached_message_is_refused_clearly(m365, exchange, tmp_path: Path) -> None:
    msg = exchange.deliver(
        ME,
        sender=BOB,
        to=[ME],
        attachments=[
            {
                "@odata.type": "#microsoft.graph.itemAttachment",
                "id": "item-1",
                "name": "Fwd: hello",
                "size": 100,
                "contentType": "",
            }
        ],
    )
    out = await _call({"message_id": msg["id"], "attachment_id": "item-1"}, _ctx(tmp_path))
    assert out["hint"] == "unsupported"
    assert "attached message" in out["error"]
    assert not (tmp_path / attachments.INBOX_DIRNAME).exists()


async def test_outlook_benchmark_makes_no_request(m365, tenant, tmp_path: Path) -> None:
    out = await _call({"message_id": "m", "attachment_id": "a"}, _ctx(tmp_path, benchmark=True))
    assert out["guard"] == "is_benchmark"
    assert tenant.requests == []


# ── classification ───────────────────────────────────────────────────


def test_the_tool_is_a_classified_mail_read() -> None:
    from robothor.engine.benchmark_sandbox import EXTERNAL_SIDE_EFFECT_TOOLS
    from robothor.engine.tools.schemas import get_engine_schemas

    assert TOOL == "gws_gmail_get_attachment"
    assert TOOL in c.MAIL_READ_TOOLS
    assert TOOL in c.READONLY_TOOLS  # no external side effect: plan mode may read
    assert TOOL in c.WORKSPACE_TOOLS
    assert TOOL in gws.HANDLERS
    assert TOOL in EXTERNAL_SIDE_EFFECT_TOOLS  # a benchmark never reaches real mail
    assert TOOL not in c.OPT_IN_TOOLS  # main's default set carries it
    schema = get_engine_schemas()[TOOL]["function"]
    assert set(schema["parameters"]["required"]) == {"message_id", "attachment_id"}
