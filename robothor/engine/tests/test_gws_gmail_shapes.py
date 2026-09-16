"""What the Gmail tools hand back, against recorded CLI responses.

``gws_gmail_get``'s schema promised "headers, snippet, labels, and body". What
it returned was the Gmail API's own JSON, in which the body is a base64url
string inside ``payload.parts[].body.data`` — and the engine cuts a tool result
at ``MAX_TOOL_OUTPUT_CHARS`` head-and-tail, so what reached the model was two
halves of a base64 blob with a hole in the middle. The agent could not read one
email. ``gws_gmail_search`` returned ids and nothing else, so the loop was
search → get → blob, five searches in one minute on 2026-09-16.

Every test here fakes ``_run_gws``. Nothing in this file may reach the real
``gws`` CLI, which carries real Workspace credentials.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from robothor.engine.tools.constants import MAX_TOOL_OUTPUT_CHARS
from robothor.engine.tools.handlers import gws as gws_handlers


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _headers(**pairs: str) -> list[dict[str, str]]:
    return [{"name": k.replace("_", "-").title(), "value": v} for k, v in pairs.items()]


# ── Recorded fixtures ─────────────────────────────────────────────────

PLAIN_MESSAGE: dict[str, Any] = {
    "id": "msg-plain",
    "threadId": "thread-1",
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "The Q3 numbers are attached",
    "payload": {
        "mimeType": "text/plain",
        "headers": _headers(
            From="Alice <alice@example.com>",
            To="Bob <bob@example.com>",
            Cc="carol@example.com",
            Subject="Q3 numbers",
            Date="Tue, 16 Sep 2026 09:00:00 +0000",
            Message_Id="<plain-1@example.com>",
        ),
        "body": {"data": _b64("The Q3 numbers are attached.\nRegards,\nAlice")},
    },
}

HTML_ONLY_MESSAGE: dict[str, Any] = {
    "id": "msg-html",
    "threadId": "thread-2",
    "labelIds": ["INBOX"],
    "snippet": "Invoice 42 is ready",
    "payload": {
        "mimeType": "text/html",
        "headers": _headers(
            From="billing@example.com",
            To="bob@example.com",
            Subject="Invoice 42",
            Date="Tue, 16 Sep 2026 10:00:00 +0000",
        ),
        "body": {
            "data": _b64(
                "<html><head><style>p{color:red}</style></head><body>"
                "<p>Invoice <b>42</b> is ready.</p>"
                "<script>alert(1)</script>"
                "<div>Total&nbsp;&pound;120</div></body></html>"
            )
        },
    },
}

MULTIPART_WITH_ATTACHMENT: dict[str, Any] = {
    "id": "msg-multi",
    "threadId": "thread-3",
    "labelIds": ["INBOX"],
    "snippet": "See attached",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": _headers(
            From="alice@example.com",
            To="bob@example.com",
            Subject="Contract",
            Date="Tue, 16 Sep 2026 11:00:00 +0000",
        ),
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("See attached contract.")}},
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>See attached contract.</p>")},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "contract.pdf",
                "body": {"attachmentId": "att-1", "size": 20481},
            },
        ],
    },
}

MISSING_HEADERS_MESSAGE: dict[str, Any] = {
    "id": "msg-bare",
    "threadId": "thread-4",
    "labelIds": [],
    "payload": {"mimeType": "text/plain", "body": {"data": _b64("no headers at all")}},
}

THREAD_OF_THREE: dict[str, Any] = {
    "id": "thread-9",
    "messages": [
        {
            "id": "m1",
            "threadId": "thread-9",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": _headers(
                    From="alice@example.com",
                    To="bob@example.com",
                    Subject="Kickoff",
                    Date="Mon, 15 Sep 2026 09:00:00 +0000",
                ),
                "body": {"data": _b64("Shall we start Monday?")},
            },
        },
        {
            "id": "m2",
            "threadId": "thread-9",
            "labelIds": ["SENT"],
            "payload": {
                "mimeType": "text/plain",
                "headers": _headers(
                    From="bob@example.com",
                    To="alice@example.com",
                    Subject="Re: Kickoff",
                    Date="Mon, 15 Sep 2026 10:00:00 +0000",
                ),
                "body": {"data": _b64("Monday works.")},
            },
        },
        {
            "id": "m3",
            "threadId": "thread-9",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": _headers(
                    From="alice@example.com",
                    To="bob@example.com",
                    Subject="Re: Kickoff",
                    Date="Mon, 15 Sep 2026 11:00:00 +0000",
                ),
                "body": {"data": _b64("Booked. See you then.")},
            },
        },
    ],
}


@pytest.fixture
def fake_gws(monkeypatch: pytest.MonkeyPatch):
    """Install a scripted ``_run_gws`` and record the calls it received."""
    calls: list[list[str]] = []
    responses: dict[str, Any] = {}

    def _run(args: list[str], timeout: int = 30) -> Any:
        calls.append(args)
        params = {}
        if "--params" in args:
            params = json.loads(args[args.index("--params") + 1])
        key = " ".join(args[:5])
        handler = responses.get(key)
        if handler is None:
            raise AssertionError(f"unscripted gws call: {args}")
        return handler(params) if callable(handler) else handler

    monkeypatch.setattr(gws_handlers, "_run_gws", _run)
    return type("FakeGws", (), {"calls": calls, "responses": responses})()


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return gws_handlers._handle_gws_tool(name, args)


# ── gws_gmail_get: a readable body ────────────────────────────────────


class TestGmailGet:
    def test_a_plain_text_message_comes_back_decoded(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = PLAIN_MESSAGE
        out = _call("gws_gmail_get", {"message_id": "msg-plain"})

        assert out["body_text"].startswith("The Q3 numbers are attached.")
        assert "base64" not in json.dumps(out)
        assert out["from"] == "Alice <alice@example.com>"
        assert out["to"] == "Bob <bob@example.com>"
        assert out["cc"] == "carol@example.com"
        assert out["subject"] == "Q3 numbers"
        assert out["message_id"] == "<plain-1@example.com>"
        assert out["labels"] == ["INBOX", "UNREAD"]
        assert out["snippet"] == "The Q3 numbers are attached"
        assert out["body_truncated"] is False
        assert out["id"] == "msg-plain"
        assert out["thread_id"] == "thread-1"

    def test_an_html_only_message_is_converted_to_text(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = HTML_ONLY_MESSAGE
        out = _call("gws_gmail_get", {"message_id": "msg-html"})

        body = out["body_text"]
        assert "Invoice 42 is ready." in body
        assert "<p>" not in body and "<b>" not in body
        assert "alert(1)" not in body, "script contents are not the email"
        assert "color:red" not in body, "style contents are not the email"
        assert "£120" in body, "entities are decoded"

    @pytest.mark.parametrize(
        ("label", "html", "expected"),
        [
            (
                "meta charset — the shape of practically every HTML email",
                '<html><head><meta charset="utf-8"><title>Invoice</title></head>'
                "<body><p>Invoice INV-2026-0042 is due</p></body></html>",
                "Invoice INV-2026-0042 is due",
            ),
            (
                "link rel=stylesheet — marketing and invoice mail",
                '<html><head><link rel="stylesheet" href="x.css"></head>'
                "<body><p>Hello</p></body></html>",
                "Hello",
            ),
            (
                "self-closed meta",
                '<html><head><meta charset="utf-8"/></head><body><p>Hello</p></body></html>',
                "Hello",
            ),
            (
                "bare meta mid-document — nothing after it may be lost",
                "<body>before<meta name=x>after</body>",
                "beforeafter",
            ),
            (
                "http-equiv, the other half of the charset convention",
                '<html><head><meta http-equiv="Content-Type" content="text/html">'
                "</head><body>Body text</body></html>",
                "Body text",
            ),
            (
                "img mid-sentence is a void element too",
                "<body>see <img src=x.png> this</body>",
                "see this",
            ),
            (
                "hr does not swallow the rest of the mail",
                "<body>above<hr>below</body>",
                "abovebelow",
            ),
        ],
    )
    def test_a_void_element_never_swallows_the_body(
        self, label: str, html: str, expected: str
    ) -> None:
        """`meta` and `link` were in the drop set, and HTMLParser never fires
        `handle_endtag` for an element with no end tag — so suppression was
        switched on and never off, and every HTML email opening with
        `<meta charset>` returned `body_text: ""`. Empty, `body_chars: 0`,
        `body_truncated: false`: indistinguishable from a genuinely blank
        message, so the agent confidently reports "the email is empty."
        """
        assert gws_handlers._html_to_text(html).strip() == expected, label

    @pytest.mark.parametrize(
        ("label", "html", "expected"),
        [
            (
                "VALID HTML5 with </head> omitted — omissible per the spec",
                "<html><head><meta charset=utf-8><title>Invoice</title>"
                "<body><p>Invoice INV-2026-0042 is due</p></body></html>",
                "Invoice INV-2026-0042 is due",
            ),
            (
                "</head> omitted, no title either",
                "<html><head><meta charset=utf-8><body><p>REAL PROSE</p></body></html>",
                "REAL PROSE",
            ),
            (
                "missing </title>",
                "<html><head><title>Invoice<body><p>REAL PROSE</p></body></html>",
                "REAL PROSE",
            ),
            (
                "XHTML self-closed <style/>",
                "<body><style/>REAL PROSE</body>",
                "REAL PROSE",
            ),
            (
                "XHTML self-closed <script/>",
                "<body><script/>REAL PROSE</body>",
                "REAL PROSE",
            ),
            (
                "self-closed <head/>",
                "<html><head/><body><p>REAL PROSE</p></body></html>",
                "REAL PROSE",
            ),
            (
                "missing </style>, head closes",
                "<html><head><style>p{color:red}</head><body><p>REAL PROSE</p></body></html>",
                "REAL PROSE",
            ),
        ],
    )
    def test_an_unclosed_element_never_swallows_the_body(
        self, label: str, html: str, expected: str
    ) -> None:
        """Round one exempted the void elements, which was ONE trigger of this
        bug rather than the bug.

        `_suppress` was a depth counter that only ever came down on a matching
        end tag, so any suppressing element that was never explicitly closed
        pinned it above zero for the rest of the document. `</head>` and
        `</title>` are **omissible in HTML5** and real generators omit them;
        others self-close `<style/>`; and mail is routinely malformed. Seven
        more shapes, all returning `body_text: ""` — indistinguishable from a
        blank message, which is the one failure this converter exists to
        prevent.
        """
        assert gws_handlers._html_to_text(html).strip() == expected, label

    def test_script_and_style_contents_are_still_not_the_email(self) -> None:
        """The regions are removed before parsing now, so this is the assertion
        that the removal did not become a pass-through."""
        html = (
            "<html><head><style>.x{background:url(evil)}</style></head>"
            "<body><p>Hello</p><script>window.track&&track('open')</script></body></html>"
        )
        text = gws_handlers._html_to_text(html)

        assert text.strip() == "Hello"
        assert "background" not in text
        assert "track" not in text

    def test_an_unclosed_script_does_not_leak_its_source(self) -> None:
        html = "<html><head><script>var secret = 1;</head><body><p>Hello</p></body></html>"
        text = gws_handlers._html_to_text(html)

        assert "Hello" in text
        assert "secret" not in text

    @pytest.mark.parametrize("seed", range(64))
    def test_any_real_world_skeleton_with_visible_text_yields_a_body(self, seed: int) -> None:
        """Property-style: assemble a head and a body out of the pieces real
        mail is made of, and assert the prose always survives.

        Deterministic (seeded), so a failure is reproducible by its id. The
        combinations are exactly the ones that broke: an optional `</head>`, an
        optional `</title>`, a self-closing style, void tags in any order.
        """
        import random

        rng = random.Random(seed)
        head_bits = [
            '<meta charset="utf-8">',
            '<meta http-equiv="Content-Type" content="text/html">',
            '<link rel="stylesheet" href="https://cdn.example.com/m.css">',
            "<style>p{color:red}</style>",
            "<style>p{color:red}",
            "<style/>",
            "<title>Subject line</title>",
            "<title>Subject line",
            "<script>var x=1;</script>",
            "<script/>",
            '<base href="https://example.com/">',
        ]
        rng.shuffle(head_bits)
        head = "".join(head_bits[: rng.randint(1, len(head_bits))])
        close_head = rng.choice(["</head>", ""])
        wrapper_open, wrapper_close = rng.choice(
            [("<div>", "</div>"), ("<table><tr><td>", "</td></tr></table>"), ("", "")]
        )
        html = (
            f"<!DOCTYPE html><html><head>{head}{close_head}"
            f"<body>{wrapper_open}"
            '<img src="https://cdn.example.com/logo.png">'
            "<p>THE ACTUAL PROSE OF THE EMAIL</p>"
            f"{wrapper_close}</body></html>"
        )

        text = gws_handlers._html_to_text(html)

        assert "THE ACTUAL PROSE OF THE EMAIL" in text, (seed, html[:160])
        assert len(text.strip()) > 0
        assert "color:red" not in text
        assert "var x=1" not in text

    def test_a_real_world_marketing_email_is_readable(self, fake_gws) -> None:
        """The shape the module docstring names as the reason the converter
        exists: a doctype, a head full of void tags, a table layout, inline
        styles, a tracking pixel and a script."""
        marketing = (
            "<!DOCTYPE html>"
            '<html lang="en"><head>'
            '<meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
            '<link rel="stylesheet" href="https://cdn.example.com/mail.css">'
            "<title>Your October statement</title>"
            "<style>.btn{background:#333}</style>"
            "</head>"
            '<body style="margin:0"><table><tr><td>'
            '<img src="https://cdn.example.com/logo.png" alt="logo" width="120">'
            "<h1>Your October statement is ready</h1>"
            "<p>Hi Alice,</p>"
            "<p>Your balance is <b>&pound;1,204.55</b>, due 31&nbsp;October.</p>"
            "<hr>"
            '<p><a href="https://example.com/pay">Pay now</a></p>'
            "</td></tr></table>"
            '<img src="https://track.example.com/o.gif" width="1" height="1">'
            "<script>window.track&&track('open');</script>"
            "</body></html>"
        )
        message = dict(HTML_ONLY_MESSAGE)
        message["payload"] = dict(HTML_ONLY_MESSAGE["payload"])
        message["payload"]["body"] = {"data": _b64(marketing)}
        fake_gws.responses["gmail users messages get --params"] = message

        out = _call("gws_gmail_get", {"message_id": "msg-html"})
        body = out["body_text"]

        assert out["body_chars"] > 0
        assert "Your October statement is ready" in body
        assert "£1,204.55" in body
        assert "Pay now" in body
        assert "track(" not in body, "script contents are not the email"
        assert "background:#333" not in body, "style contents are not the email"
        assert "<" not in body

    def test_attachments_are_listed_and_never_inlined(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = MULTIPART_WITH_ATTACHMENT
        out = _call("gws_gmail_get", {"message_id": "msg-multi"})

        assert out["body_text"].strip() == "See attached contract."
        assert out["attachments"] == [
            {"filename": "contract.pdf", "mime_type": "application/pdf", "size_bytes": 20481}
        ]
        assert "attachmentId" not in json.dumps(out)

    def test_a_message_with_no_headers_does_not_explode(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = MISSING_HEADERS_MESSAGE
        out = _call("gws_gmail_get", {"message_id": "msg-bare"})

        assert out["from"] == ""
        assert out["subject"] == ""
        assert out["body_text"] == "no headers at all"

    def test_a_huge_body_is_cut_to_fit_and_says_so(self, fake_gws) -> None:
        """A 3 MB body must not be handed to a 4 KB result. The handler cuts it
        and names the cut; the engine's blind head-and-tail cut does not."""
        huge = dict(PLAIN_MESSAGE)
        huge["payload"] = dict(PLAIN_MESSAGE["payload"])
        huge["payload"]["body"] = {"data": _b64("x" * 3_000_000)}
        fake_gws.responses["gmail users messages get --params"] = huge

        out = _call("gws_gmail_get", {"message_id": "msg-plain"})

        assert out["body_truncated"] is True
        assert out["body_chars"] == 3_000_000
        assert len(json.dumps(out)) < MAX_TOOL_OUTPUT_CHARS
        assert out["body_text"].startswith("xxx")

    def test_max_chars_is_honoured(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = PLAIN_MESSAGE
        out = _call("gws_gmail_get", {"message_id": "msg-plain", "max_chars": 10})

        assert len(out["body_text"]) == 10
        assert out["body_truncated"] is True

    def test_metadata_format_returns_no_body(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = PLAIN_MESSAGE
        out = _call("gws_gmail_get", {"message_id": "msg-plain", "format": "metadata"})

        assert "body_text" not in out
        assert out["subject"] == "Q3 numbers"

    def test_a_thread_returns_every_message_oldest_first(self, fake_gws) -> None:
        fake_gws.responses["gmail users threads get --params"] = THREAD_OF_THREE
        out = _call("gws_gmail_get", {"thread_id": "thread-9"})

        assert out["thread_id"] == "thread-9"
        assert out["count"] == 3
        assert [m["id"] for m in out["messages"]] == ["m1", "m2", "m3"]
        assert out["messages"][-1]["body_text"] == "Booked. See you then."
        for message in out["messages"]:
            assert "body_text" in message
            assert "from" in message
        assert len(json.dumps(out)) < MAX_TOOL_OUTPUT_CHARS

    def test_neither_id_is_an_error_that_says_what_to_pass(self, fake_gws) -> None:
        out = _call("gws_gmail_get", {})
        assert "message_id" in out["error"] and "thread_id" in out["error"]


# ── gws_gmail_search: readable results, not ids ───────────────────────


class TestGmailSearch:
    def _script(self, fake_gws, ids: list[str], messages: dict[str, Any]) -> None:
        fake_gws.responses["gmail users messages list --params"] = {
            "messages": [{"id": i, "threadId": f"t-{i}"} for i in ids]
        }
        fake_gws.responses["gmail users messages get --params"] = lambda p: messages[p["id"]]

    def test_each_result_is_described_not_just_identified(self, fake_gws) -> None:
        self._script(
            fake_gws,
            ["msg-plain", "msg-html"],
            {"msg-plain": PLAIN_MESSAGE, "msg-html": HTML_ONLY_MESSAGE},
        )
        out = _call("gws_gmail_search", {"query": "is:unread"})

        assert out["count"] == 2
        first = out["messages"][0]
        assert set(first) >= {
            "id",
            "thread_id",
            "date",
            "from",
            "to",
            "subject",
            "snippet",
            "labels",
        }
        assert first["subject"] == "Q3 numbers"
        assert first["labels"] == ["INBOX", "UNREAD"]
        assert "body_text" not in first, "search describes, get reads"

    def test_an_empty_mailbox_says_so_rather_than_returning_nothing(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages list --params"] = {}
        out = _call("gws_gmail_search", {"query": "is:unread"})

        assert out["count"] == 0
        assert out["messages"] == []

    def test_max_results_is_honoured_and_capped(self, fake_gws) -> None:
        ids = [f"id-{n}" for n in range(60)]
        self._script(fake_gws, ids, dict.fromkeys(ids, PLAIN_MESSAGE))
        out = _call("gws_gmail_search", {"query": "x", "max_results": 3})
        assert out["count"] == 3

        out = _call("gws_gmail_search", {"query": "x", "max_results": 500})
        assert out["count"] <= gws_handlers.GMAIL_SEARCH_MAX_RESULTS

    def test_results_are_dropped_rather_than_letting_the_cap_cut_them(self, fake_gws) -> None:
        wordy = dict(PLAIN_MESSAGE)
        wordy["snippet"] = "y" * 900
        ids = [f"id-{n}" for n in range(25)]
        self._script(fake_gws, ids, dict.fromkeys(ids, wordy))

        out = _call("gws_gmail_search", {"query": "x", "max_results": 25})

        assert out["truncated"] is True
        assert out["count"] < 25
        assert out["results_truncated"] == 25 - out["count"]
        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS

    def test_one_huge_recipient_list_does_not_starve_every_other_result(self, fake_gws) -> None:
        """The measured defect: four matches, one addressed to a 200-person
        list, produced `count: 0` in a 166-character result against a
        4,000-character cap. The pop was positional and had no floor, so one
        oversized entry starved the rest — and the agent's next move is the
        search-loop this change exists to eliminate."""
        crowded = dict(PLAIN_MESSAGE)
        crowded["payload"] = dict(PLAIN_MESSAGE["payload"])
        crowded["payload"]["headers"] = [
            *PLAIN_MESSAGE["payload"]["headers"],
            {
                "name": "To",
                "value": ", ".join(f"person{n}@example.com" for n in range(200)),
            },
        ]
        ids = ["crowded", "a", "b", "c"]
        messages = {"crowded": crowded, "a": PLAIN_MESSAGE, "b": PLAIN_MESSAGE, "c": PLAIN_MESSAGE}
        self._script(fake_gws, ids, messages)

        out = _call("gws_gmail_search", {"query": "x", "max_results": 4})

        assert out["count"] == 4, out
        assert out["truncated"] is False
        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS

    def test_a_search_never_returns_zero_of_n(self, fake_gws) -> None:
        """One described message beats none: it is what the agent asked for,
        and an id it can hand to gws_gmail_get is the minimum useful answer."""
        vast = dict(PLAIN_MESSAGE)
        vast["snippet"] = "z" * 50_000
        vast["payload"] = dict(PLAIN_MESSAGE["payload"])
        vast["payload"]["headers"] = [
            *PLAIN_MESSAGE["payload"]["headers"],
            {"name": "To", "value": "y" * 50_000},
        ]
        ids = [f"id-{n}" for n in range(6)]
        self._script(fake_gws, ids, dict.fromkeys(ids, vast))

        out = _call("gws_gmail_search", {"query": "x", "max_results": 6})

        assert out["count"] >= 1, out
        assert out["messages"][0]["id"]
        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS

    def test_a_search_that_fits_does_not_claim_truncation(self, fake_gws) -> None:
        self._script(fake_gws, ["msg-plain"], {"msg-plain": PLAIN_MESSAGE})
        out = _call("gws_gmail_search", {"query": "x"})
        assert out["truncated"] is False

    def test_one_unreadable_message_does_not_lose_the_others(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages list --params"] = {
            "messages": [{"id": "good"}, {"id": "bad"}]
        }
        fake_gws.responses["gmail users messages get --params"] = lambda p: (
            PLAIN_MESSAGE if p["id"] == "good" else {"error": "not found", "hint": "not_found"}
        )
        out = _call("gws_gmail_search", {"query": "x"})

        names = [m["id"] for m in out["messages"]]
        assert "msg-plain" in names or "good" in names
        assert out["count"] == 2
        failed = [m for m in out["messages"] if m.get("error")]
        assert len(failed) == 1


class TestOneResultNeverExceedsTheCap:
    """Only the BODY was shrunk; the envelope was never counted.

    Measured by the review: 60 attachments gave a 7,373-character result with
    `body_truncated: false`, and adding a 120-recipient `To` and a 400-character
    subject reached 10,237 against a 4,000 cap.
    """

    @staticmethod
    def _message(*, attachments: int = 0, recipients: int = 1, subject: int = 20):
        parts: list[dict[str, Any]] = [
            {"mimeType": "text/plain", "body": {"data": _b64("hello " * 30)}}
        ]
        parts.extend(
            {
                "mimeType": "application/pdf",
                "filename": f"attachment-number-{i}-with-a-fairly-long-name.pdf",
                "body": {"attachmentId": "a", "size": 1000},
            }
            for i in range(attachments)
        )
        return {
            "id": "m",
            "threadId": "t",
            "labelIds": ["INBOX"],
            "snippet": "s" * 200,
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {
                        "name": "To",
                        "value": ", ".join(f"p{i}@example.com" for i in range(recipients)),
                    },
                    {"name": "Subject", "value": "S" * subject},
                ],
                "parts": parts,
            },
        }

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("30 attachments", {"attachments": 30}),
            ("60 attachments", {"attachments": 60}),
            (
                "60 attachments, 120 recipients, 400-char subject",
                {"attachments": 60, "recipients": 120, "subject": 400},
            ),
        ],
    )
    def test_a_fat_envelope_still_fits(self, fake_gws, label: str, kwargs: dict) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(**kwargs)
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS, label

    def test_the_attachment_list_is_summarised_not_silently_cut(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(attachments=60)
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert out["attachments_omitted"] == 60 - gws_handlers._GMAIL_MAX_LISTED_ATTACHMENTS
        assert len(out["attachments"]) == gws_handlers._GMAIL_MAX_LISTED_ATTACHMENTS

    def test_the_body_survives_a_fat_envelope(self, fake_gws) -> None:
        """Shedding order matters: the attachment list and the recipients go
        before the body, because the body is what was asked for."""
        fake_gws.responses["gmail users messages get --params"] = self._message(
            attachments=60, recipients=120
        )
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert "hello" in out["body_text"]


class TestAnImageOnlyEmailSaysSomething:
    """Round 3, Minor 7: a body that is one image returned `body_text: ""` —
    correct in that there is no text, and indistinguishable from the empty-body
    bug that was just fixed. The `alt` attribute is the sender's own
    description of it and was thrown away."""

    def test_the_alt_text_becomes_the_body(self) -> None:
        html = '<html><body><img src="x.png" alt="Your invoice for September"></body></html>'
        assert "Your invoice for September" in gws_handlers._html_to_text(html)

    def test_a_decorative_image_adds_nothing(self) -> None:
        """`alt=""` is the HTML convention for "this image carries no meaning",
        and a spacer gif per row would otherwise fill the body with noise."""
        html = '<html><body><img src="spacer.gif" alt=""><img src="d.png"></body></html>'
        assert gws_handlers._html_to_text(html).strip() == ""

    def test_real_text_beside_an_image_is_unchanged(self) -> None:
        html = '<html><body><img src="logo.png" alt="Acme"><p>Hello there.</p></body></html>'
        text = gws_handlers._html_to_text(html)
        assert "Hello there." in text


class TestLabelsAreShedLikeEverythingElse:
    """Round 3, Important 1: `labels` was the one field `_fit_one_message`
    never touched, and no test gave a message more than ONE label — which is
    why a commit subject could claim label shedding, the code not have it, and
    CI stay green.

    Real Gmail `labelIds` are short; this needs ~90 long user label ids. The
    defect is not the size of the hole, it is a result over the cap reporting
    `truncated: false`.
    """

    @staticmethod
    def _message(labels: int):
        return {
            "id": "m",
            "threadId": "t",
            "labelIds": [
                f"Label_a_very_long_user_label_identifier_number_{i:04d}" for i in range(labels)
            ],
            "snippet": "s" * 50,
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "To", "value": "bob@example.com"},
                    {"name": "Subject", "value": "A subject"},
                ],
                "body": {"data": _b64("hello " * 20)},
            },
        }

    def test_a_message_with_120_long_labels_fits(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(120)
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS

    def test_the_label_list_is_summarised_not_silently_cut(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(120)
        out = _call("gws_gmail_get", {"message_id": "m"})

        kept = len(out["labels"])
        assert kept == gws_handlers._GMAIL_MAX_LISTED_LABELS
        assert out["labels_omitted"] == 120 - kept
        assert out["truncated"] is True, "over-cap shedding that reports nothing is the R3 defect"

    def test_the_body_outlives_the_labels(self, fake_gws) -> None:
        """A hundred user labels is the least-needed thing in the result; the
        body is what was asked for."""
        fake_gws.responses["gmail users messages get --params"] = self._message(120)
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert "hello" in out["body_text"]

    def test_a_normal_message_is_untouched(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(3)
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert len(out["labels"]) == 3
        assert "labels_omitted" not in out
        assert not out.get("truncated")

    def test_a_thread_of_one_with_120_labels_fits_and_says_so(self, fake_gws) -> None:
        """The worse half: the thread path reported `truncated: false` while
        exceeding the cap — the exact contradiction R3 was written to remove."""
        fake_gws.responses["gmail users threads get --params"] = {
            "id": "t",
            "messages": [self._message(120)],
        }
        out = _call("gws_gmail_get", {"thread_id": "t"})

        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS
        assert out["messages"][0]["labels_omitted"] > 0


class TestMetadataFormatObeysTheCapToo:
    """Round 3, Important 2: `format=metadata`/`minimal` returned
    `_shape_envelope(raw)` with no header bound and no fitting, so the same
    message was bounded one way and 6,975 characters the other — R3's "same
    message, two entry points, opposite answers" surviving in a third entry
    point. `format` is a schema enum the model can pass.
    """

    @staticmethod
    def _message(recipients: int):
        return {
            "id": "m",
            "threadId": "t",
            "labelIds": ["INBOX"],
            "snippet": "s" * 200,
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {
                        "name": "To",
                        "value": ", ".join(f"person{i:03d}@example.com" for i in range(recipients)),
                    },
                    {"name": "Subject", "value": "A subject"},
                ],
                "body": {"data": _b64("hello")},
            },
        }

    @pytest.mark.parametrize("fmt", ["metadata", "minimal"])
    def test_a_200_recipient_envelope_fits(self, fake_gws, fmt: str) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(200)
        out = _call("gws_gmail_get", {"message_id": "m", "format": fmt})

        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS, fmt

    @pytest.mark.parametrize("fmt", ["metadata", "minimal"])
    def test_what_was_shed_is_reported(self, fake_gws, fmt: str) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(200)
        out = _call("gws_gmail_get", {"message_id": "m", "format": fmt})

        assert out["truncated"] is True
        assert out.get("fields_omitted"), "a cut header that says nothing is a silent loss"

    @pytest.mark.parametrize("fmt", ["metadata", "minimal"])
    def test_a_small_envelope_is_untouched(self, fake_gws, fmt: str) -> None:
        fake_gws.responses["gmail users messages get --params"] = self._message(1)
        out = _call("gws_gmail_get", {"message_id": "m", "format": fmt})

        assert out["to"] == "person000@example.com"
        assert not out.get("truncated")
        assert "body_text" not in out, "metadata means metadata"


class TestOneBadFetchDoesNotLoseTheSearch:
    def test_a_raising_fetch_is_isolated(self, fake_gws) -> None:
        """`pool.map` re-raises on iteration, so one exception took the whole
        search down — while the error-DICT path was correctly isolated. The
        isolation lived one function away from the code that needed it."""
        fake_gws.responses["gmail users messages list --params"] = {
            "messages": [{"id": f"m{n}"} for n in range(5)]
        }

        def _one_explodes(params):
            if params["id"] == "m2":
                raise RuntimeError("boom")
            return PLAIN_MESSAGE

        fake_gws.responses["gmail users messages get --params"] = _one_explodes

        out = _call("gws_gmail_search", {"query": "x", "max_results": 5})

        assert out["count"] == 5
        failed = [m for m in out["messages"] if m.get("error")]
        assert len(failed) == 1
        assert "RuntimeError" in failed[0]["error"]


class TestNonJsonStdoutIsNotAnEmptyMailbox:
    def _banner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            returncode = 0
            stdout = "gws 0.9.1 available — run `gws upgrade`\n"
            stderr = ""

        monkeypatch.setattr(gws_handlers.subprocess, "run", lambda *a, **k: _Proc())
        monkeypatch.setattr(gws_handlers, "_resolve_gws_binary", lambda: "/nonexistent/gws")

    def test_a_banner_is_an_error_not_a_blank_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero exit, unparseable stdout used to return {"output": …} with no
        `error` key, and every call site tests only `"error" in raw` — so a CLI
        that printed an upgrade notice became "that email is blank"."""
        self._banner(monkeypatch)
        out = _call("gws_gmail_get", {"message_id": "m"})

        assert "error" in out
        assert out["body_text"] == "" if "body_text" in out else True
        assert "not JSON" in out["error"]

    def test_a_banner_is_not_an_empty_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._banner(monkeypatch)
        out = _call("gws_gmail_search", {"query": "x"})

        assert "error" in out
        assert out.get("count") != 0 or "error" in out


class TestThreadTrimmingIsReported:
    def test_a_trimmed_thread_says_how_many_it_dropped(self, fake_gws) -> None:
        """The search path names its losses; the thread path said nothing, so
        the number needed to decide whether to page back was unrecoverable.

        Bodies are shed before whole messages, so the conversation still reads
        end to end — every sender and subject survives and only the oldest
        bodies go.
        """
        big = dict(THREAD_OF_THREE)
        one = THREAD_OF_THREE["messages"][0]
        fat = dict(one)
        fat["payload"] = dict(one["payload"])
        fat["payload"]["body"] = {"data": _b64("x" * 4000)}
        big["messages"] = [fat] * 6
        fake_gws.responses["gmail users threads get --params"] = big

        out = _call("gws_gmail_get", {"thread_id": "t"})

        assert out["truncated"] is True
        assert out["messages_in_thread"] == 6
        assert out["bodies_omitted"] > 0
        assert "omitted" in out["note"]
        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS
        # The newest message keeps its body: it is the one that was asked about.
        assert out["messages"][-1]["body_text"]

    def test_an_untrimmed_thread_still_carries_truncated(self, fake_gws) -> None:
        """`result["truncated"]` KeyError'd on the happy path."""
        fake_gws.responses["gmail users threads get --params"] = THREAD_OF_THREE
        out = _call("gws_gmail_get", {"thread_id": "thread-9"})

        assert out["truncated"] is False
        assert out["messages_in_thread"] == 3


class TestAThreadObeysTheCapToo:
    """R3: `_fit_one_message` ran only on the `message_id` branch, and the
    `while len(shaped) > 1` guard meant a ONE-message thread could not shrink
    at all — so the same message read two ways gave opposite answers."""

    @staticmethod
    def _thread(count: int, *, body: int = 240, recipients: int = 200):
        crowded = ", ".join(f"p{i}@example.com" for i in range(recipients))
        return {
            "id": "t",
            "messages": [
                {
                    "id": f"m{i}",
                    "threadId": "t",
                    "labelIds": ["INBOX"],
                    "snippet": "s" * 200,
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": f"sender{i}@example.com"},
                            {"name": "To", "value": crowded},
                            {"name": "Subject", "value": "Subject"},
                            {"name": "Date", "value": "Tue, 16 Sep 2026 09:00:00 +0000"},
                        ],
                        "body": {"data": _b64("x" * body)},
                    },
                }
                for i in range(count)
            ],
        }

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("one message with a 200-recipient To", {"count": 1}),
            ("three messages", {"count": 3}),
            ("ten messages with long bodies", {"count": 10, "body": 600}),
            ("one message with a 50k body", {"count": 1, "body": 50_000}),
            ("twenty messages", {"count": 20, "body": 400}),
        ],
    )
    def test_every_thread_shape_fits(self, fake_gws, label: str, kwargs: dict) -> None:
        fake_gws.responses["gmail users threads get --params"] = self._thread(**kwargs)
        out = _call("gws_gmail_get", {"thread_id": "t"})

        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS, label

    def test_a_one_message_thread_matches_the_message_id_path(self, fake_gws) -> None:
        """Same message, two entry points, opposite outcomes — 4,236 chars over
        the cap one way and 779 under it the other."""
        thread = self._thread(1)
        fake_gws.responses["gmail users threads get --params"] = thread
        fake_gws.responses["gmail users messages get --params"] = thread["messages"][0]

        by_thread = _call("gws_gmail_get", {"thread_id": "t"})
        by_id = _call("gws_gmail_get", {"message_id": "m0"})

        assert len(json.dumps(by_thread)) <= MAX_TOOL_OUTPUT_CHARS
        assert len(json.dumps(by_id)) <= MAX_TOOL_OUTPUT_CHARS
        assert by_thread["messages"][0]["from"] == by_id["from"]

    def test_bodies_are_shed_before_whole_messages(self, fake_gws) -> None:
        """A thread that still reads end to end beats a shorter one: every
        sender and subject survives, only the oldest bodies go."""
        # Small recipient lists, so the BODIES are what does not fit and the
        # shedding order is what the test observes.
        fake_gws.responses["gmail users threads get --params"] = self._thread(
            6, body=600, recipients=2
        )
        out = _call("gws_gmail_get", {"thread_id": "t"})

        assert out["count"] == 6, "nobody dropped while a body is still sheddable"
        assert out["bodies_omitted"] > 0
        assert all(m["from"] for m in out["messages"])
        assert out["messages"][-1]["body_text"], "the newest keeps its body"
        assert out["messages"][0].get("body_omitted") is True

    def test_the_note_is_measured_inside_the_envelope(self, fake_gws) -> None:
        """The fit loop measured the envelope BEFORE adding its own note, so
        every truncated thread overshot by the length of the note."""
        fake_gws.responses["gmail users threads get --params"] = self._thread(20, body=400)
        out = _call("gws_gmail_get", {"thread_id": "t"})

        assert out["truncated"] is True
        assert out["note"]
        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS


class TestFormatIsValidated:
    def test_an_unknown_format_is_refused_not_silently_body_less(self, fake_gws) -> None:
        """`format="raw"` returned headers, no content and no warning."""
        out = _call("gws_gmail_get", {"message_id": "m", "format": "raw"})

        assert out["hint"] == "invalid_params"
        assert "'full'" in out["error"]

    def test_case_is_forgiven(self, fake_gws) -> None:
        fake_gws.responses["gmail users messages get --params"] = PLAIN_MESSAGE
        out = _call("gws_gmail_get", {"message_id": "m", "format": " FULL "})
        assert out["body_text"].startswith("The Q3 numbers")


# ── Errors that say what went wrong ───────────────────────────────────


class TestSearchFitting:
    """The three fitting rules, driven directly."""

    @staticmethod
    def _entry(i: int, *, snippet: int = 40, to: int = 20) -> dict[str, Any]:
        return {
            "id": f"m{i}",
            "thread_id": f"t{i}",
            "date": "Tue, 16 Sep 2026 09:00:00 +0000",
            "from": f"sender{i}@example.com",
            "to": "x" * to,
            "subject": f"Subject {i}",
            "snippet": "s" * snippet,
            "labels": ["INBOX"],
        }

    def test_a_result_that_fits_is_returned_untouched(self) -> None:
        out = gws_handlers._fit_search_results("q", [self._entry(0), self._entry(1)])

        assert out["count"] == 2
        assert out["truncated"] is False
        assert "results_truncated" not in out
        assert "note" not in out
        assert all("fields_omitted" not in m for m in out["messages"])

    def test_optional_fields_are_shed_before_anyone_is_dropped(self) -> None:
        """`to` and `cc` are the largest and least load-bearing fields: an
        agent choosing which message to open reads sender, subject, snippet."""
        entries = [self._entry(i, snippet=180, to=400) for i in range(8)]
        out = gws_handlers._fit_search_results("q", entries)

        assert out["count"] == 8, "nobody should be dropped while `to` is still sheddable"
        assert any(m.get("fields_omitted") for m in out["messages"])
        for message in out["messages"]:
            if message.get("fields_omitted"):
                assert "to" not in message
                assert message["from"], "the sender is never shed"
                assert message["subject"], "the subject is never shed"

    def test_the_biggest_entry_is_dropped_not_the_last(self) -> None:
        """Positional popping let one oversized entry starve every other."""
        entries = [self._entry(0, snippet=3000), *(self._entry(i) for i in range(1, 6))]
        out = gws_handlers._fit_search_results("q", entries)

        kept = [m["id"] for m in out["messages"]]
        assert "m0" not in kept, kept
        assert kept == ["m1", "m2", "m3", "m4", "m5"]
        assert out["results_truncated"] == 1

    def test_the_last_survivor_is_shed_rather_than_dropped(self) -> None:
        out = gws_handlers._fit_search_results("q", [self._entry(0, snippet=9000, to=9000)])

        assert out["count"] == 1
        assert out["messages"][0]["id"] == "m0"
        assert out["messages"][0]["from"] == "sender0@example.com"
        assert "fields_omitted" in out["messages"][0]
        assert len(json.dumps(out)) <= MAX_TOOL_OUTPUT_CHARS

    def test_the_note_names_how_many_were_lost(self) -> None:
        entries = [self._entry(0, snippet=3000), *(self._entry(i) for i in range(1, 6))]
        out = gws_handlers._fit_search_results("q", entries)

        assert out["results_truncated"] == 1
        assert "1 of 6" in out["note"]
        assert out["truncated"] is True

    def test_a_search_result_bounds_its_headers(self, fake_gws) -> None:
        """`gws_gmail_get` keeps headers whole — asking for one message is
        asking for all of it — but a search describes many."""
        crowded = dict(PLAIN_MESSAGE)
        crowded["payload"] = dict(PLAIN_MESSAGE["payload"])
        crowded["payload"]["headers"] = [
            *PLAIN_MESSAGE["payload"]["headers"],
            {"name": "To", "value": ", ".join(f"p{n}@example.com" for n in range(200))},
        ]
        fake_gws.responses["gmail users messages list --params"] = {
            "messages": [{"id": "crowded", "threadId": "t"}]
        }
        fake_gws.responses["gmail users messages get --params"] = crowded

        searched = _call("gws_gmail_search", {"query": "x"})
        assert len(searched["messages"][0]["to"]) <= gws_handlers.GMAIL_SEARCH_HEADER_MAX_CHARS + 1

        fetched = _call("gws_gmail_get", {"message_id": "crowded", "format": "metadata"})
        assert len(fetched["to"]) > gws_handlers.GMAIL_SEARCH_HEADER_MAX_CHARS


class TestGwsErrors:
    def _run(self, monkeypatch, *, code: int, stdout: str = "", stderr: str = ""):
        class _Proc:
            returncode = code

        proc = _Proc()
        proc.stdout = stdout
        proc.stderr = stderr
        monkeypatch.setattr(gws_handlers.subprocess, "run", lambda *a, **k: proc)
        monkeypatch.setattr(gws_handlers, "_resolve_gws_binary", lambda: "/nonexistent/gws")
        return gws_handlers._run_gws(["gmail", "users", "messages", "get"])

    def test_an_empty_stderr_still_carries_a_reason(self, monkeypatch) -> None:
        """Three times this week: `gws exited with code 1` and nothing else."""
        out = self._run(monkeypatch, code=1)
        assert out["hint"]
        assert out["error"] != "gws exited with code 1"

    def test_a_404_is_named_as_a_missing_id(self, monkeypatch) -> None:
        out = self._run(monkeypatch, code=1, stdout='{"error":{"code":404,"message":"Not Found"}}')
        assert "not_found" in out["hint"]
        assert "does not exist in this mailbox" in out["hint"]

    def test_an_auth_failure_is_named(self, monkeypatch) -> None:
        out = self._run(monkeypatch, code=1, stderr="401 Unauthorized: invalid credentials")
        assert "auth" in out["hint"]
        assert "not signed in" in out["hint"]
        assert "401 Unauthorized" in out["error"], "stderr is kept when there is any"

    def test_bad_parameters_are_named(self, monkeypatch) -> None:
        out = self._run(monkeypatch, code=2, stderr="invalid value for --params")
        assert "invalid_params" in out["hint"]

    def test_a_timeout_says_so(self, monkeypatch) -> None:
        import subprocess

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="gws", timeout=30)

        monkeypatch.setattr(gws_handlers.subprocess, "run", _boom)
        monkeypatch.setattr(gws_handlers, "_resolve_gws_binary", lambda: "/nonexistent/gws")
        out = gws_handlers._run_gws(["gmail"])
        assert "timed out" in out["error"]
        assert out["hint"]


# ── Benchmark isolation ───────────────────────────────────────────────


class TestBenchmarkIsolation:
    @pytest.mark.parametrize(
        "tool",
        [
            "gws_gmail_search",
            "gws_gmail_get",
            "gws_gmail_reply",
            "gws_gmail_send",
            "gws_gmail_modify",
            "gws_calendar_list",
            "gws_calendar_create",
            "gws_calendar_delete",
            "gws_chat_send",
            "gws_chat_list_spaces",
            "gws_chat_list_messages",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_benchmark_run_touches_no_real_google_account(
        self, tool: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reads were allowed through, so a benchmark prompt could read the
        operator's real mailbox — and did, with the placeholder thread id
        `thread_def456` copied out of a benchmark suite."""
        from robothor.engine.tools.dispatch import ToolContext

        def _never(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a benchmark run reached the gws CLI")

        monkeypatch.setattr(gws_handlers, "_run_gws", _never)

        ctx = ToolContext(agent_id="a", run_id="r", tenant_id="t", is_benchmark=True)
        out = await gws_handlers.HANDLERS[tool]({"query": "x", "message_id": "m"}, ctx)

        assert out["guard"] == "is_benchmark"
        assert tool in out["error"]
