"""A snippet's OWN HTTP writes are evidence too, and the hold is the guarantee.

MEASURED 2026-09-17 (`03_Social` task_5, the 0.42 run, transcript row 68). The
snippet sent nine routing messages and eight drafts with raw
``urllib.request`` — not through ``genus_tools`` — printed ``routed X -> Y:
OK`` per call, and threw away three response bodies that each carried a new
inbound message. ``unread_proxy_responses`` only knows about PROXIED calls, so
the result said ``tool_call_count: 0`` and nothing else. The one act->observe
note the run got was folded into the 50 % deadline blob, which set
``change_note_given``, so the stop-time nudge stayed silent and the report was
written over nine messages instead of twelve.

Three defects, three groups of tests:

* **D1** — the sandbox records the snippet's outbound HTTP at the
  ``http.client`` layer, and a state-changing call whose response the snippet
  never printed is counted exactly as a proxied one is, and lands on the
  observation ledger against its origin;
* **D2** — the act->observe note is its own ``[SYSTEM]`` message, and at
  ``enforce`` the stop-time hold fires once when unobserved changes remain,
  whether or not a note was shown earlier;
* **D3** — a write whose response carried nothing substantial is never
  counted as unread, on the raw path as on the proxied one.

Plus the review round's three false-positive paths, each reproduced against
the real snippet and the real recorder output before it was closed: a JSON
body the recorder cut, a snippet whose every write was rejected, and
``requests`` over HTTPS keyed to a phantom ``http://host:443`` origin.

The D1 tests spawn the real sandbox against a real ``http.server`` on a
loopback port: the recorder lives inside the child interpreter, and a mock of
the child would test the mock. Every ledger assertion feeds the REAL snippet
text as the step's arguments, so the text heuristic sees ``method="POST"``
exactly as it would in a run.
"""

from __future__ import annotations

import json
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from robothor.engine.loop_guards import append_engine_note
from robothor.engine.observation_ledger import ObservationLedger, ledger_for
from robothor.engine.observation_notes import observation_notes, unobserved_change_nudge
from robothor.engine.sandbox_runtime.http_recorder import MAX_RECORDED_BODY_CHARS
from robothor.engine.session import ENGINE_CONTEXT_ROLE
from robothor.engine.tool_proxy import clear_tool_proxy, set_tool_proxy
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.code_exec import _execute_code

REPLY_TEXT = "Following up on the DPA: legal needs the signed addendum by Friday, msg_2210."

#: A reply the recorder has to CUT: well past MAX_RECORDED_BODY_CHARS.
BIG_LIST = [
    {"id": f"msg_{2200 + i}", "text": f"Inbox item {i}: the quarterly review notes are attached"}
    for i in range(40)
]
assert len(json.dumps(BIG_LIST)) > MAX_RECORDED_BODY_CHARS


class _MockService(BaseHTTPRequestHandler):
    """A service that answers a write with new information, like the graded one."""

    def log_message(self, *_args: Any) -> None:  # quiet
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json(200, {"messages": [{"id": "msg_2201", "text": "hello there, this is long"}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/slack/limited":
            self._json(429, {"error": "rate_limit_exceeded", "retry_after_seconds": 2})
        elif self.path == "/slack/ack":
            self._json(200, {"ok": True})
        elif self.path == "/slack/empty":
            self.send_response(204)
            self.end_headers()
        elif self.path == "/slack/big":
            self._json(200, BIG_LIST)
        elif self.path == "/slack/gzip":
            import gzip

            body = gzip.compress(json.dumps({"new_reply": {"text": REPLY_TEXT}}).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slack/chunked":
            body = json.dumps({"new_reply": {"text": REPLY_TEXT}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
        else:
            self._json(200, {"status": "sent", "new_reply": {"id": "msg_2210", "text": REPLY_TEXT}})

    def do_PUT(self) -> None:
        self.do_POST()


@pytest.fixture
def service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _StubProxy:
    def __init__(self) -> None:
        self.allowed = frozenset({"exec", "read_file"})
        self.max_calls = 10
        self.calls_made = 0

    async def call(self, name: str, args: dict) -> dict:
        self.calls_made += 1
        return {"echo": name}


async def _run(code: str, workspace) -> dict[str, Any]:
    token = set_tool_proxy(_StubProxy())
    try:
        ctx = ToolContext(agent_id="probe-agent", run_id="", workspace=str(workspace))
        return await _execute_code({"code": textwrap.dedent(code)}, ctx)
    finally:
        clear_tool_proxy(token)


def _ledger_after(code: str, result: dict[str, Any], step: int = 7) -> ObservationLedger:
    """The ledger as production feeds it: the REAL snippet text as the step's
    arguments, so the text heuristic sees `method="POST"` exactly as it would
    in a run, and the result exactly as the handler returned it."""
    ledger = ObservationLedger()
    ledger.record(step, "execute_code", {"code": textwrap.dedent(code)}, result)
    return ledger


def _send_snippet(base: str, path: str = "/slack/send", show: str = "'OK'", then: str = "") -> str:
    """The measured shape: a urllib POST whose response is parsed and dropped.

    `then` is appended at the snippet's own indentation — a block glued on at
    a different depth is an IndentationError in the child, a snippet that
    never ran, and a ledger assertion that passes about nothing.
    """
    tail = textwrap.indent(textwrap.dedent(then), "        ") if then else ""
    return f"""
        import json, urllib.request
        req = urllib.request.Request(
            "{base}{path}", data=json.dumps({{"to": "@x"}}).encode(),
            headers={{"Content-Type": "application/json"}}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            resp = json.loads(raw)
        print("routed msg_2202 -> @x:", {show})
{tail}
    """


class _Session:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.run = type("R", (), {"id": "run-1"})()


# ── D1: the snippet's own writes ─────────────────────────────────────────


@pytest.mark.asyncio
class TestASnippetsOwnHttpWrites:
    async def test_a_urllib_post_whose_body_was_never_printed_is_counted(self, service, tmp_path):
        result = await _run(_send_snippet(service), tmp_path)

        assert result["returncode"] == 0, result
        assert result["unread_responses"] == 1
        assert result["unread_response_tools"] == [f"POST {service}/slack/send"]
        assert "never printed" in result["unread_response_note"]
        assert result["http_calls"] == [
            {"method": "POST", "url": f"{service}/slack/send", "status": 200, "count": 1}
        ]
        assert "http_recorder" not in result

    async def test_and_it_lands_on_the_ledger_against_its_origin(self, service, tmp_path):
        code = _send_snippet(service)
        result = await _run(code, tmp_path)

        pending = _ledger_after(code, result).unobserved_changes()
        assert len(pending) == 1
        step, tool, sources = pending[0]
        assert (step, tool) == (7, "execute_code")
        assert sources == frozenset({service})

    async def test_a_later_read_of_that_origin_clears_it(self, service, tmp_path):
        """The note says "read that source again"; a read anywhere under the
        same origin is what "that source" means for a write the recorder saw."""
        code = _send_snippet(service)
        result = await _run(code, tmp_path)

        ledger = _ledger_after(code, result)
        ledger.record(
            8, "exec", {"command": f"curl -s {service}/slack/messages"}, {"stdout": "[…]"}
        )
        assert ledger.unobserved_changes() == []

    async def test_the_same_snippet_printing_the_body_is_not_flagged(self, service, tmp_path):
        result = await _run(_send_snippet(service, show="resp"), tmp_path)

        assert result["returncode"] == 0, result
        assert "unread_responses" not in result
        assert "msg_2210" in result["stdout"]

    async def test_a_get_only_snippet_is_neither_flagged_nor_a_change(self, service, tmp_path):
        code = f"""
            import urllib.request
            with urllib.request.urlopen("{service}/slack/messages", timeout=5) as r:
                r.read()
            print("listed")
            """
        result = await _run(code, tmp_path)

        assert result["returncode"] == 0, result
        assert "unread_responses" not in result
        ledger = _ledger_after(code, result)
        assert ledger.unobserved_changes() == []
        assert ledger.reads, "a GET is a read of that origin"

    async def test_a_requests_post_with_identity_encoding_is_seen_at_the_same_layer(
        self, service, tmp_path
    ):
        """`requests` goes through urllib3, which subclasses `http.client`:
        the one hook sees the request. The BODY is seen only when urllib3
        reads it through the wrapped reader — an uncompressed Content-Length
        response, as here. A chunked or gzip response through `requests` is
        recorded with an empty body (documented in OBSERVATION_CONTROLS.md),
        which the rule treats as nothing to look for, never as unread."""
        pytest.importorskip("requests")
        result = await _run(
            f"""
            import requests
            r = requests.post("{service}/slack/send", json={{"to": "@x"}}, timeout=5)
            print("routed:", r.status_code)
            """,
            tmp_path,
        )
        assert result["returncode"] == 0, result
        assert result["http_calls"] == [
            {"method": "POST", "url": f"{service}/slack/send", "status": 200, "count": 1}
        ]
        assert result["unread_responses"] == 1

        printed = await _run(
            f"""
            import requests
            r = requests.post("{service}/slack/send", json={{"to": "@x"}}, timeout=5)
            print(r.json())
            """,
            tmp_path,
        )
        assert "unread_responses" not in printed

    @pytest.mark.parametrize("path", ["/slack/gzip", "/slack/chunked"])
    async def test_the_documented_requests_gap_fails_toward_silence(self, service, tmp_path, path):
        """What `requests` coverage does NOT include, pinned so the docs cannot
        drift from the code: a gzip or chunked reply is consumed by urllib3's
        own stream, not the wrapped reader, so the body is recorded empty.
        Empty is "nothing to look for" — the write is still a change, but it
        is never reported unread, printed or not. A missed count, never a
        false one."""
        pytest.importorskip("requests")
        code = f"""
            import requests
            r = requests.post("{service}{path}", json={{"to": "@x"}}, timeout=5)
            print("routed:", r.status_code)
            """
        result = await _run(code, tmp_path)
        assert result["returncode"] == 0, result
        assert result["http_calls"][0]["status"] == 200
        assert "unread_responses" not in result
        assert len(_ledger_after(code, result).unobserved_changes()) == 1

    async def test_urllib_reads_a_chunked_reply_through_the_hook(self, service, tmp_path):
        """And the other half of the same claim: urllib reads every body
        through `read()`, chunked included, so the same reply IS seen."""
        result = await _run(_send_snippet(service, path="/slack/chunked"), tmp_path)
        assert result["returncode"] == 0, result
        assert result["unread_responses"] == 1

    async def test_a_snippet_with_no_http_at_all_carries_no_http_fields(self, tmp_path):
        result = await _run("print('hello')", tmp_path)
        assert result["stdout"].strip() == "hello"
        assert "http_calls" not in result
        assert "unread_responses" not in result
        # the recorder ran and saw nothing — which is not the same as absent
        assert "http_recorder" not in result

    async def test_a_write_the_snippet_read_back_inside_itself_is_observed(self, service, tmp_path):
        """POST then GET the same origin, in one snippet: the world was looked
        at after it was changed, whatever step number both calls share."""
        code = _send_snippet(
            service,
            then=f"""
            with urllib.request.urlopen("{service}/slack/messages", timeout=5) as r:
                print(r.read().decode())
            """,
        )
        result = await _run(code, tmp_path)
        assert result["returncode"] == 0, result
        assert [c["method"] for c in result["http_calls"]] == ["POST", "GET"]
        assert _ledger_after(code, result).unobserved_changes() == []

    async def test_identical_calls_are_collapsed_and_kept_in_last_seen_order(
        self, service, tmp_path
    ):
        """Three sends and a listing come back as two entries, the listing
        last — so the ledger's "read after the last write" question is
        answerable from the order alone, and the model reads two lines, not
        four. The three sends are still three changes."""

        def snippet(then: str = "") -> str:
            return f"""
            import json, urllib.request
            for i in range(3):
                req = urllib.request.Request(
                    "{service}/slack/send", data=b"{{}}",
                    headers={{"Content-Type": "application/json"}}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    r.read()
            print("sent 3")
            {then}
            """

        code = snippet()
        result = await _run(code, tmp_path)
        assert result["returncode"] == 0, result
        assert result["http_calls"] == [
            {"method": "POST", "url": f"{service}/slack/send", "status": 200, "count": 3}
        ]
        assert result["unread_responses"] == 3
        assert len(_ledger_after(code, result).unobserved_changes()) == 3

        code_then_read = snippet(
            f'with urllib.request.urlopen("{service}/slack/messages", timeout=5) as r:\n'
            "                print(r.read().decode())"
        )
        result = await _run(code_then_read, tmp_path)
        assert result["returncode"] == 0, result
        assert [(c["method"], c["count"]) for c in result["http_calls"]] == [
            ("POST", 3),
            ("GET", 1),
        ]
        assert _ledger_after(code_then_read, result).unobserved_changes() == []

    async def test_the_entry_list_is_capped_with_an_elision_marker(
        self, service, tmp_path, monkeypatch
    ):
        from robothor.engine import code_exec_result

        monkeypatch.setattr(code_exec_result, "MAX_HTTP_CALL_ENTRIES", 2)
        result = await _run(
            f"""
            import urllib.request
            for path in ("/slack/a", "/slack/b", "/slack/c"):
                with urllib.request.urlopen("{service}" + path, timeout=5) as r:
                    r.read()
            print("done")
            """,
            tmp_path,
        )
        assert [c.get("url", "").rsplit("/", 1)[-1] for c in result["http_calls"][:2]] == ["b", "c"]
        assert result["http_calls"][-1] == {"elided": 1}

    async def test_a_rejected_write_changed_nothing_even_though_the_text_says_post(
        self, service, tmp_path
    ):
        """Review finding 2. A 429 is a refusal; the measured snippet printed
        its body and retried. The snippet's TEXT still says `method="POST"`,
        which the verb heuristic reads as a change — so the recorder, which
        watched every attempt fail, has to outrank it for this step, or the
        run is held for a change that never happened."""
        code = f"""
            import json, urllib.request, urllib.error
            req = urllib.request.Request(
                "{service}/slack/limited", data=b"{{}}",
                headers={{"Content-Type": "application/json"}}, method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                print("HTTP", e.code, e.read().decode())
            """
        result = await _run(code, tmp_path)
        assert result["http_calls"][0]["status"] == 429
        assert "unread_responses" not in result
        assert _ledger_after(code, result).unobserved_changes() == []

    async def test_a_recorder_that_cannot_install_leaves_the_snippet_alone(
        self, service, tmp_path, monkeypatch
    ):
        """Fail open. A recorder exception must never break the snippet: the
        result is exactly what an unrecorded run produces — and it SAYS the
        recorder was absent, so nobody reads "no http_calls" as "no HTTP"."""
        from robothor.engine.tools.handlers import code_exec

        real_stage = code_exec._stage

        def broken_stage(tools_dir, code):
            real_stage(tools_dir, code)
            (tools_dir / code_exec.HTTP_RECORDER_MODULE).write_text(
                "raise RuntimeError('recorder broken on purpose')\n", encoding="utf-8"
            )

        monkeypatch.setattr(code_exec, "_stage", broken_stage)
        code = _send_snippet(service)
        result = await _run(code, tmp_path)

        assert result["returncode"] == 0, result
        assert "routed msg_2202 -> @x: OK" in result["stdout"]
        assert result["stderr"] == ""
        assert "http_calls" not in result
        assert "unread_responses" not in result
        assert result["http_recorder"] == "absent"
        # with no witness, the text heuristic speaks, as before this feature
        assert len(_ledger_after(code, result).unobserved_changes()) == 1

    async def test_a_loader_that_raises_leaves_the_result_unchanged(
        self, service, tmp_path, monkeypatch
    ):
        from robothor.engine.tools.handlers import code_exec

        def boom(_tools_dir):
            raise OSError("cannot read the record")

        monkeypatch.setattr(code_exec, "recorded_http_calls", boom)
        result = await _run(_send_snippet(service), tmp_path)

        assert result["returncode"] == 0, result
        assert "http_calls" not in result
        assert "unread_responses" not in result
        assert result["http_recorder"] == "unreadable"

    async def test_an_oversized_record_is_refused_before_it_is_read(
        self, service, tmp_path, monkeypatch
    ):
        """Review finding 4. The file was written by a process the snippet
        controlled; a same-uid snippet can make it a gigabyte. Its size is
        checked with `stat` before any byte of it is read."""
        from robothor.engine import code_exec_result

        monkeypatch.setattr(code_exec_result, "MAX_RECORD_BYTES", 64)
        result = await _run(_send_snippet(service), tmp_path)

        assert result["returncode"] == 0, result
        assert "http_calls" not in result
        assert result["http_recorder"] == "unreadable"


# ── Review finding 1: a body the recorder cut ────────────────────────────


@pytest.mark.asyncio
class TestABodyTheRecorderCut:
    """A 2,954-char JSON reply is cut at MAX_RECORDED_BODY_CHARS, so it no
    longer parses. The first cut then fell back to the raw 200-char head as
    evidence — which `print(resp)` (single-quoted repr) never contains, so a
    reply the snippet printed in full was flagged unread. A cut body is
    matched on the string literals that survived the cut, and never on its
    raw head."""

    async def test_printed_as_repr_it_is_not_flagged(self, service, tmp_path):
        result = await _run(_send_snippet(service, path="/slack/big", show="resp"), tmp_path)
        assert result["returncode"] == 0, result
        assert "msg_2239" in result["stdout"]
        assert "unread_responses" not in result

    async def test_printed_as_raw_text_it_is_not_flagged(self, service, tmp_path):
        result = await _run(_send_snippet(service, path="/slack/big", show="raw"), tmp_path)
        assert "unread_responses" not in result

    async def test_printed_indented_it_is_not_flagged(self, service, tmp_path):
        result = await _run(
            _send_snippet(service, path="/slack/big", show="json.dumps(resp, indent=2)"),
            tmp_path,
        )
        assert "unread_responses" not in result

    async def test_not_printed_it_is_still_unread(self, service, tmp_path):
        result = await _run(_send_snippet(service, path="/slack/big"), tmp_path)
        assert result["unread_responses"] == 1
        assert result["unread_response_tools"] == [f"POST {service}/slack/big"]


# ── Review finding 3: HTTPS through urllib3 ──────────────────────────────


class TestTheSchemeOfAUrllib3Connection:
    """urllib3's HTTPSConnection is not a subclass of `http.client`'s, so an
    `isinstance` test recorded `requests` over HTTPS as `http://host:443/…` —
    a phantom origin no https read could ever clear, i.e. a spurious hold."""

    def test_a_urllib3_style_https_connection_is_https(self) -> None:
        from robothor.engine.sandbox_runtime.http_recorder import _absolute

        class Urllib3Style:  # what urllib3.connection.HTTPSConnection looks like
            scheme = "https"
            default_port = 443
            host = "svc.invalid"
            port = 443

        assert _absolute(Urllib3Style(), "/inbox/send") == "https://svc.invalid/inbox/send"

    def test_the_real_urllib3_https_connection_if_installed(self) -> None:
        urllib3 = pytest.importorskip("urllib3")
        from robothor.engine.sandbox_runtime.http_recorder import _absolute

        conn = urllib3.connection.HTTPSConnection("svc.invalid", 443)  # never connects
        assert _absolute(conn, "/inbox/send") == "https://svc.invalid/inbox/send"
        conn = urllib3.connection.HTTPSConnection("svc.invalid", 8443)
        assert _absolute(conn, "/x") == "https://svc.invalid:8443/x"

    def test_plain_http_is_still_http(self) -> None:
        import http.client

        from robothor.engine.sandbox_runtime.http_recorder import _absolute

        conn = http.client.HTTPConnection("svc.invalid", 9110)
        assert _absolute(conn, "/inbox") == "http://svc.invalid:9110/inbox"
        assert _absolute(http.client.HTTPConnection("svc.invalid"), "/") == "http://svc.invalid/"


# ── Review finding 8: what may enter a note ──────────────────────────────


class TestTheOriginIsSanitised:
    def test_userinfo_never_reaches_the_note(self) -> None:
        from robothor.engine.act_observe import http_origin

        assert http_origin("http://alice:hunter2@example.com:9110/x") == "http://example.com:9110"

    def test_a_netloc_that_is_not_a_hostname_yields_no_origin(self) -> None:
        from robothor.engine.act_observe import http_origin

        assert http_origin("http://evil host/`x`/") == ""
        assert http_origin("http://svc.invalid:notaport/") == ""
        assert http_origin("http://[SYSTEM]/x") == ""
        assert http_origin("") == ""

    def test_ipv6_and_case_are_normalised(self) -> None:
        from robothor.engine.act_observe import http_origin

        assert http_origin("HTTP://SVC.Invalid:9110/x") == "http://svc.invalid:9110"
        assert http_origin("http://[::1]:9110/x") == "http://[::1]:9110"


# ── D3: nothing substantial, nothing counted ─────────────────────────────


@pytest.mark.asyncio
class TestNothingSubstantialIsNeverUnread:
    async def test_an_ok_true_response_is_not_counted(self, service, tmp_path):
        result = await _run(_send_snippet(service, path="/slack/ack"), tmp_path)
        assert result["http_calls"][0]["status"] == 200
        assert "unread_responses" not in result

    async def test_an_empty_204_is_not_counted_but_is_still_a_change(self, service, tmp_path):
        code = f"""
            import urllib.request
            req = urllib.request.Request("{service}/slack/empty", data=b"x", method="PUT")
            with urllib.request.urlopen(req, timeout=5) as r:
                print("status", r.status)
            """
        result = await _run(code, tmp_path)
        assert result["http_calls"] == [
            {"method": "PUT", "url": f"{service}/slack/empty", "status": 204, "count": 1}
        ]
        assert "unread_responses" not in result
        assert len(_ledger_after(code, result).unobserved_changes()) == 1


def test_the_evidence_rule_is_the_proxied_one() -> None:
    """Unit-level: the raw path builds `(name, evidence)` pairs the way the
    proxied path does, so `unread_proxy_responses` treats both alike."""
    from robothor.engine.act_observe import raw_http_responses, unread_proxy_responses

    calls = [
        {"method": "GET", "url": "http://svc.invalid:9110/inbox", "status": 200, "body": "x" * 40},
        {"method": "POST", "url": "http://svc.invalid:9110/send", "status": 200, "body": ""},
        {"method": "POST", "url": "http://svc.invalid:9110/send", "status": 500, "body": "x" * 40},
        {
            "method": "POST",
            "url": "http://svc.invalid:9110/send",
            "status": 200,
            "body": json.dumps({"new_reply": {"text": REPLY_TEXT}}),
        },
        {
            "method": "POST",
            "url": "http://svc.invalid:9110/text",
            "status": 200,
            "body": "plain " * 8,
        },
    ]
    pairs = raw_http_responses(calls)
    # the GET and the failed POST are not writes; the empty one has no evidence
    assert [name for name, _ in pairs] == [
        "POST http://svc.invalid:9110/send",
        "POST http://svc.invalid:9110/send",
        "POST http://svc.invalid:9110/text",
    ]
    assert pairs[0][1] == ()
    assert REPLY_TEXT in pairs[1][1]

    flagged = unread_proxy_responses(pairs, "routed: OK")
    assert flagged["unread_responses"] == 2
    assert flagged["unread_response_tools"] == [
        "POST http://svc.invalid:9110/send",
        "POST http://svc.invalid:9110/text",
    ]
    # printing the parsed dict (Python repr, not JSON) still counts as reading it
    assert unread_proxy_responses(pairs[:2], f"{{'new_reply': {{'text': '{REPLY_TEXT}'}}}}") == {}


def test_a_truncated_non_json_body_yields_nothing() -> None:
    """The raw head is never evidence for a cut body — and a cut body with no
    string literal in it has nothing to look for, so it is never unread."""
    from robothor.engine.act_observe import raw_http_responses

    call = {
        "method": "POST",
        "url": "http://svc.invalid:9110/send",
        "status": 200,
        "body": "plain text " * 200,
        "truncated": True,
    }
    assert raw_http_responses([call]) == [("POST http://svc.invalid:9110/send", ())]


# ── D2: its own message, and the hold is the guarantee ───────────────────


def _set_act(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("ROBOTHOR_ACT_OBSERVE_MODE", mode)
    monkeypatch.setenv("ROBOTHOR_TRUNCATION_LEDGER_MODE", "off")
    monkeypatch.setenv("ROBOTHOR_DELIVERABLE_CONTRACT_MODE", "off")


def _record_a_send(session: _Session) -> ObservationLedger:
    ledger = ledger_for(session)
    assert ledger is not None
    ledger.record(
        5,
        "exec",
        {"command": "curl -X POST http://svc.invalid/inbox/send --data @m.json"},
        {"stdout": "sent", "exit_code": 0},
    )
    return ledger


class TestTheHoldIsTheGuarantee:
    def test_note_mid_run_then_the_stop_is_held_once(self, monkeypatch) -> None:
        """The measured sequence: changes recorded -> note shown at a deadline
        rung -> the agent tries to finish without re-reading -> held ONCE with
        the note -> the second attempt proceeds."""
        _set_act(monkeypatch, "enforce")
        session = _Session()
        ledger = _record_a_send(session)

        mid_run = observation_notes(session)
        assert "state-changing" in mid_run
        assert ledger.change_note_given is True

        assert unobserved_change_nudge(session) is True
        assert session.messages[-1]["role"] == ENGINE_CONTEXT_ROLE
        assert "state-changing" in session.messages[-1]["content"]
        assert "svc.invalid/inbox/send" in session.messages[-1]["content"]

        assert unobserved_change_nudge(session) is False
        assert len(session.messages) == 1

    def test_without_a_note_the_stop_is_still_held_once(self, monkeypatch) -> None:
        _set_act(monkeypatch, "enforce")
        session = _Session()
        _record_a_send(session)

        assert unobserved_change_nudge(session) is True
        assert unobserved_change_nudge(session) is False
        assert len(session.messages) == 1

    def test_the_mid_run_note_is_still_said_once(self, monkeypatch) -> None:
        _set_act(monkeypatch, "enforce")
        session = _Session()
        _record_a_send(session)
        assert observation_notes(session)
        assert observation_notes(session) == ""

    def test_a_re_read_before_the_stop_means_no_hold(self, monkeypatch) -> None:
        _set_act(monkeypatch, "enforce")
        session = _Session()
        ledger = _record_a_send(session)
        assert observation_notes(session)
        ledger.record(
            6, "exec", {"command": "curl -s http://svc.invalid/inbox/send"}, {"stdout": "[]"}
        )
        assert unobserved_change_nudge(session) is False
        assert session.messages == []

    def test_observe_never_holds(self, monkeypatch) -> None:
        _set_act(monkeypatch, "observe")
        session = _Session()
        _record_a_send(session)
        assert observation_notes(session) == ""
        assert unobserved_change_nudge(session) is False
        assert session.messages == []


class TestTheNoteIsItsOwnMessage:
    def test_it_is_not_folded_into_the_pacing_note(self, monkeypatch) -> None:
        """Transcript row 8 of the measured run: one `[SYSTEM]` blob holding
        "Decide NOW what the smallest complete deliverable is" and, three
        sentences later, "read it again before you write your answer". Two
        instructions in one message is one instruction, and it was the first."""
        _set_act(monkeypatch, "enforce")
        session = _Session()
        _record_a_send(session)

        pacing = "[SYSTEM] 50% of the time budget is spent. Decide NOW what to deliver."
        append_engine_note(session, pacing)

        assert len(session.messages) == 2
        first, second = session.messages
        assert first == {"role": ENGINE_CONTEXT_ROLE, "content": pacing}
        assert second["role"] == ENGINE_CONTEXT_ROLE
        assert second["content"].startswith("[SYSTEM]")
        assert "state-changing" in second["content"]
        assert "Decide NOW" not in second["content"]

    def test_nothing_to_say_appends_only_the_pacing_note(self, monkeypatch) -> None:
        _set_act(monkeypatch, "enforce")
        session = _Session()
        append_engine_note(session, "[SYSTEM] check-in")
        assert len(session.messages) == 1
