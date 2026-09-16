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


# ── Errors that say what went wrong ───────────────────────────────────


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
