"""Attachment ids in the listing, and ``get_attachment`` on both mail providers.

Gmail through a scripted gws CLI, Microsoft 365 through the fake Exchange
tenant. A provider hands back ``{"filename", "mime_type", "size_bytes",
"data"}`` or the usual ``{"error", "hint"}`` dict; an async provider's
:class:`~robothor.workspace.errors.WorkspaceError` becomes that dict at the
blocking bridge, as for every other mail call.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from robothor.engine.tools.handlers import gws
from robothor.workspace.errors import NotFound, Unsupported
from robothor.workspace.google import gmail_parse
from robothor.workspace.google.adapter import GoogleMail, GoogleMailSync
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.microsoft.mail import GraphMail
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_mail import FakeExchangeMail, install_mail

ME = "assistant@example.com"
BOB = "bob@example.com"
PDF = b"%PDF-1.4 pretend"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _gmail_message() -> dict[str, Any]:
    return {
        "id": "msg-1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "snippet": "see attached",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": BOB}],
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "text/plain",
                    "body": {"data": _b64url(b"see attached")},
                },
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "contract.pdf",
                    "body": {"attachmentId": "ANGjdJ-long-opaque-1", "size": len(PDF)},
                },
                {
                    "partId": "2",
                    "mimeType": "text/csv",
                    "filename": "small.csv",
                    "body": {"data": _b64url(b"a,b\n1,2\n"), "size": 8},
                },
            ],
        },
    }


# ── the listing ──────────────────────────────────────────────────────


def test_gmail_listing_carries_the_stable_part_id() -> None:
    listed = gmail_parse._extract_attachments(_gmail_message()["payload"])
    assert listed == [
        {
            "filename": "contract.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(PDF),
            "attachment_id": "1",
        },
        {"filename": "small.csv", "mime_type": "text/csv", "size_bytes": 8, "attachment_id": "2"},
    ]


def test_gmail_listing_falls_back_to_the_attachment_id_without_a_part_id() -> None:
    payload = {
        "parts": [
            {"filename": "a.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "x"}}
        ]
    }
    (listed,) = gmail_parse._extract_attachments(payload)
    assert listed["attachment_id"] == "x"


def test_a_listing_with_no_id_at_all_keeps_the_old_shape() -> None:
    payload = {"parts": [{"filename": "a.pdf", "mimeType": "application/pdf", "body": {}}]}
    assert gmail_parse._extract_attachments(payload) == [
        {"filename": "a.pdf", "mime_type": "application/pdf", "size_bytes": 0}
    ]


# ── Gmail get_attachment ─────────────────────────────────────────────


class FakeCli:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        self.calls.append(list(args))
        verb = " ".join(args[:5])
        params = json.loads(args[args.index("--params") + 1])
        if verb == "gmail users messages get --params":
            assert params["format"] == "full"
            return self.message
        if verb == "gmail users messages attachments get":
            assert params["messageId"] == "msg-1"
            assert params["id"] == "ANGjdJ-long-opaque-1"
            return {"size": len(PDF), "data": _b64url(PDF)}
        raise AssertionError(f"unscripted gws call: {args}")


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    fake = FakeCli(_gmail_message())
    monkeypatch.setattr(gws, "_run_gws", fake)
    return fake


def test_gmail_downloads_by_part_id(cli: FakeCli) -> None:
    got = GoogleMailSync().get_attachment("msg-1", "1")
    assert got == {
        "filename": "contract.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(PDF),
        "data": PDF,
    }
    assert [" ".join(c[:5]) for c in cli.calls] == [
        "gmail users messages get --params",
        "gmail users messages attachments get",
    ]


def test_gmail_accepts_the_raw_attachment_id_too(cli: FakeCli) -> None:
    got = GoogleMailSync().get_attachment("msg-1", "ANGjdJ-long-opaque-1")
    assert got["data"] == PDF


def test_gmail_inline_part_needs_no_second_call(cli: FakeCli) -> None:
    got = GoogleMailSync().get_attachment("msg-1", "2")
    assert got["data"] == b"a,b\n1,2\n"
    assert got["filename"] == "small.csv"
    assert len(cli.calls) == 1


def test_gmail_unknown_attachment_is_not_found(cli: FakeCli) -> None:
    got = GoogleMailSync().get_attachment("msg-1", "9")
    assert got["hint"].startswith("not_found")
    assert "contract.pdf" in got["error"]  # names what IS there
    assert len(cli.calls) == 1


def test_gmail_refuses_an_oversized_part_before_downloading(cli: FakeCli) -> None:
    got = GoogleMailSync().get_attachment("msg-1", "1", max_bytes=4)
    assert got["hint"] == "unsupported"
    assert "was not downloaded" in got["error"]
    assert len(cli.calls) == 1  # never fetched the bytes


async def test_gmail_async_face_delegates(cli: FakeCli) -> None:
    got = await GoogleMail().get_attachment("msg-1", "1")
    assert got["data"] == PDF


def test_gmail_message_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gws, "_run_gws", lambda *a, **k: {"error": "boom", "hint": "auth"})
    assert GoogleMailSync().get_attachment("msg-1", "1") == {"error": "boom", "hint": "auth"}


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
async def mail(tenant: FakeGraphTenant, exchange: FakeExchangeMail):
    graph = GraphClient(StaticToken(tenant), transport=tenant.transport())
    yield GraphMail(ME, graph=graph)
    await graph.aclose()


def _file(aid: str, name: str, data: bytes, mime: str = "application/pdf") -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": aid,
        "name": name,
        "contentType": mime,
        "size": len(data),
        "isInline": False,
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }


async def test_graph_listing_carries_the_attachment_id(mail, exchange, tenant) -> None:
    msg = exchange.deliver(
        ME, sender=BOB, to=[ME], attachments=[_file("AAMk+att/1=", "q3.pdf", PDF)]
    )
    shaped = mail.shape_message(await mail.get_message(msg["id"], fmt="full"), max_chars=100)
    assert shaped["attachments"] == [
        {
            "filename": "q3.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(PDF),
            "attachment_id": "AAMk+att/1=",
        }
    ]
    request = tenant.requests_matching("GET", rf"/users/{ME}/messages/.+")[-1]
    assert "id" in request.url.params["$expand"].split("$select=")[1]
    assert "contentBytes" not in json.dumps(shaped)


async def test_graph_downloads_a_file_attachment(mail, exchange, tenant) -> None:
    msg = exchange.deliver(
        ME, sender=BOB, to=[ME], attachments=[_file("AAMk+att/1=", "q3.pdf", PDF)]
    )
    got = await mail.get_attachment(msg["id"], "AAMk+att/1=")
    assert got == {
        "filename": "q3.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(PDF),
        "data": PDF,
    }
    # The id is percent-encoded into ONE path segment.
    paths = [r.url.raw_path.decode() for r in tenant.requests]
    assert any("/attachments/AAMk%2Batt%2F1%3D" in p for p in paths)


@pytest.mark.parametrize(
    ("odata_type", "word"),
    [
        ("#microsoft.graph.itemAttachment", "attached message"),
        ("#microsoft.graph.referenceAttachment", "link"),
    ],
)
async def test_graph_refuses_a_non_file_attachment(mail, exchange, odata_type, word) -> None:
    msg = exchange.deliver(
        ME,
        sender=BOB,
        to=[ME],
        attachments=[
            {
                "@odata.type": odata_type,
                "id": "att-x",
                "name": "Fwd: hi",
                "size": 10,
                "contentType": "",
            }
        ],
    )
    with pytest.raises(Unsupported) as caught:
        await mail.get_attachment(msg["id"], "att-x")
    assert word in str(caught.value)


async def test_graph_refuses_an_oversized_attachment_before_fetching_bytes(
    mail, exchange, tenant
) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME], attachments=[_file("a1", "big.pdf", PDF)])
    with pytest.raises(Unsupported) as caught:
        await mail.get_attachment(msg["id"], "a1", max_bytes=4)
    assert caught.value.code == "too_large"
    reads = tenant.requests_matching("GET", r".*/attachments/.*")
    assert len(reads) == 1
    assert "$select" in reads[0].url.params  # metadata only


async def test_graph_unknown_attachment_is_not_found(mail, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    with pytest.raises(NotFound):
        await mail.get_attachment(msg["id"], "nope")


async def test_graph_empty_attachment_id_is_not_found(mail, exchange) -> None:
    msg = exchange.deliver(ME, sender=BOB, to=[ME])
    with pytest.raises(NotFound):
        await mail.get_attachment(msg["id"], " ")
