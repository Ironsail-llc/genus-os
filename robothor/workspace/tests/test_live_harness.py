"""The live Microsoft 365 smoke suite's harness, proven without a tenant.

Runs in normal CI. Covers what keeps the live suite safe by default: it is
skipped (never failed) without opt-in and credentials, and makes no network
call when skipped; the tenant allowlist refuses before any request; the
mailbox fence refuses anything outside the test mailboxes; and the capture
scrubber plus the promotion script never let real data into a fixture.
"""

from __future__ import annotations

import base64
import email.message
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from robothor.workspace.microsoft.capture import Scrubber, find_leaks
from robothor.workspace.tests.live import harness
from robothor.workspace.tests.live.harness import (
    CapturingTransport,
    FencedTransport,
    FenceViolation,
    LiveConfig,
    LiveGuardError,
    load_config,
    poll,
    require_allowed_tenant,
    require_allowed_token,
    skip_reason,
)

REPO = Path(__file__).resolve().parents[3]
LIVE_DIR = REPO / "robothor" / "workspace" / "tests" / "live"
DIRECTORY = "0f0f0f0f-1111-2222-3333-444444444444"
OTHER_DIRECTORY = "9e9e9e9e-1111-2222-3333-444444444444"
ASSISTANT = "assistant@dev.example"
OWNER = "owner@dev.example"
CANARY = "canary@dev.example"
STRANGER = "someone@elsewhere.example"
TAG = "genus-live-20261007120000-abc123"


def _config(**overrides) -> LiveConfig:
    values = {
        "assistant": ASSISTANT,
        "owner": OWNER,
        "canary": CANARY,
        "allowed": frozenset({DIRECTORY}),
        "tenant_id": DIRECTORY,
    }
    values.update(overrides)
    return LiveConfig(**values)


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    cert = tmp_path / "app.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\nnot-a-real-one\n-----END CERTIFICATE-----\n")
    env = {
        "GENUS_LIVE_M365": "1",
        "GENUS_LIVE_M365_TENANTS": DIRECTORY,
        "GENUS_LIVE_M365_TENANT_ID": DIRECTORY,
        "GENUS_LIVE_M365_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "GENUS_LIVE_M365_CERT_PATH": str(cert),
        "GENUS_LIVE_M365_ASSISTANT": ASSISTANT,
        "GENUS_LIVE_M365_OWNER": OWNER,
        "GENUS_LIVE_M365_CANARY": CANARY,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


# ── 1. skipped unless asked ─────────────────────────────────────────────────


class TestSkipReason:
    def test_no_environment_skips(self) -> None:
        reason = skip_reason({})
        assert reason is not None and "GENUS_LIVE_M365=1" in reason

    @pytest.mark.parametrize("value", ["", "0", "true", "yes", " 2 "])
    def test_only_exactly_1_enables(self, value: str) -> None:
        assert skip_reason({"GENUS_LIVE_M365": value}) is not None

    def test_enabled_without_credentials_skips(self) -> None:
        reason = skip_reason({"GENUS_LIVE_M365": "1", "GENUS_LIVE_M365_TENANT_ID": DIRECTORY})
        assert reason is not None and "no credentials" in reason

    def test_enabled_with_env_credentials_runs(self, tmp_path: Path) -> None:
        assert skip_reason(_env(tmp_path)) is None

    def test_enabled_with_a_vault_tenant_runs(self) -> None:
        env = {"GENUS_LIVE_M365": "1", "GENUS_LIVE_M365_PLATFORM_TENANT": "dev-platform"}
        assert skip_reason(env) is None


@pytest.mark.timeout(180)
class TestTheLiveModuleInCI:
    """The real module, collected by a real pytest, with sockets that explode on use."""

    def _run(self, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        (tmp_path / "no_network_plugin.py").write_text(
            "import socket\n"
            "def _refuse(*a, **k):\n"
            "    raise RuntimeError('network call from the skipped live suite')\n"
            "socket.socket.connect = _refuse\n"
            "socket.socket.connect_ex = _refuse\n"
            "socket.create_connection = _refuse\n"
            "socket.getaddrinfo = _refuse\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("GENUS_LIVE_M365")}
        env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(REPO)])
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(LIVE_DIR),
                "-p",
                "no_network_plugin",
                "-p",
                "no:cacheprovider",
                "-q",
                "-rs",
                *args,
            ],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=170,
            check=False,
        )

    def test_ci_marker_expression_collects_and_skips_every_test(self, tmp_path: Path) -> None:
        live = len(
            [
                line
                for line in (LIVE_DIR / "test_m365_smoke.py").read_text().splitlines()
                if line.startswith("async def test_")
            ]
        )
        assert live >= 20
        result = self._run(tmp_path, "-m", "not integration and not llm and not slow and not e2e")
        out = result.stdout + result.stderr
        assert result.returncode == 0, out[-3000:]
        assert f"{live} skipped" in out, out[-3000:]
        for word in ("passed", "failed", "error"):
            assert f" {word}" not in out.splitlines()[-1], out[-3000:]
        assert "set GENUS_LIVE_M365=1" in out

    def test_a_bare_pytest_deselects_the_marker(self, tmp_path: Path) -> None:
        result = self._run(tmp_path)
        out = result.stdout + result.stderr
        # Exit 5: everything deselected, nothing ran.
        assert result.returncode == 5, out[-3000:]
        assert "deselected" in out


# ── 2. the tenant allowlist ─────────────────────────────────────────────────


class TestAllowlist:
    def test_empty_allowlist_refuses(self) -> None:
        with pytest.raises(LiveGuardError, match="GENUS_LIVE_M365_TENANTS is empty"):
            require_allowed_tenant(DIRECTORY, {})

    def test_wildcard_refuses(self) -> None:
        with pytest.raises(LiveGuardError, match="wildcard"):
            require_allowed_tenant(DIRECTORY, {"GENUS_LIVE_M365_TENANTS": "*"})

    def test_unlisted_tenant_refuses(self) -> None:
        with pytest.raises(LiveGuardError, match="not in GENUS_LIVE_M365_TENANTS"):
            require_allowed_tenant(OTHER_DIRECTORY, {"GENUS_LIVE_M365_TENANTS": DIRECTORY})

    def test_listed_tenant_passes_case_and_separator_insensitive(self) -> None:
        env = {"GENUS_LIVE_M365_TENANTS": f" dev.example , {DIRECTORY.upper()}"}
        assert require_allowed_tenant(DIRECTORY, env) == DIRECTORY
        assert require_allowed_tenant("DEV.example", env) == "dev.example"

    @staticmethod
    def _token(claims: dict) -> str:
        def part(obj: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

        return f"{part({'alg': 'none'})}.{part(claims)}.sig"

    def test_token_tid_must_be_listed(self) -> None:
        env = {"GENUS_LIVE_M365_TENANTS": DIRECTORY}
        assert require_allowed_token(self._token({"tid": DIRECTORY}), env) == DIRECTORY
        with pytest.raises(LiveGuardError):
            require_allowed_token(self._token({"tid": OTHER_DIRECTORY}), env)

    @pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "x.e30.y"])
    def test_unreadable_token_refuses(self, token: str) -> None:
        with pytest.raises(LiveGuardError):
            require_allowed_token(token, {"GENUS_LIVE_M365_TENANTS": DIRECTORY})

    def test_domain_tenant_id_resolving_elsewhere_is_caught_by_tid(self) -> None:
        env = {"GENUS_LIVE_M365_TENANTS": "dev.example"}
        assert require_allowed_tenant("dev.example", env)
        with pytest.raises(LiveGuardError):
            require_allowed_token(self._token({"tid": OTHER_DIRECTORY}), env)


class TestLoadConfig:
    def test_valid_env_mode(self, tmp_path: Path) -> None:
        config = load_config(_env(tmp_path))
        assert config.assistant == ASSISTANT and config.canary == CANARY
        assert config.writable == {ASSISTANT, OWNER}
        assert "app.pem" not in repr(config)

    def test_unlisted_env_tenant_refuses_before_anything(self, tmp_path: Path) -> None:
        with pytest.raises(LiveGuardError, match="not in GENUS_LIVE_M365_TENANTS"):
            load_config(_env(tmp_path, GENUS_LIVE_M365_TENANT_ID=OTHER_DIRECTORY))

    def test_empty_allowlist_refuses_in_vault_mode_too(self) -> None:
        env = {
            "GENUS_LIVE_M365": "1",
            "GENUS_LIVE_M365_PLATFORM_TENANT": "dev-platform",
            "GENUS_LIVE_M365_ASSISTANT": ASSISTANT,
            "GENUS_LIVE_M365_OWNER": OWNER,
            "GENUS_LIVE_M365_CANARY": CANARY,
        }
        with pytest.raises(LiveGuardError, match="empty"):
            load_config(env)

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("GENUS_LIVE_M365_CANARY", "", "GENUS_LIVE_M365_CANARY is not set"),
            ("GENUS_LIVE_M365_OWNER", "not-an-address", "not an email"),
            ("GENUS_LIVE_M365_CANARY", ASSISTANT, "three different mailboxes"),
            ("GENUS_LIVE_M365_CERT_PATH", "/nonexistent/app.pem", "not a readable file"),
        ],
    )
    def test_bad_configuration_refuses(self, tmp_path, name, value, message) -> None:
        with pytest.raises(LiveGuardError, match=message):
            load_config(_env(tmp_path, **{name: value}))

    def test_not_enabled_is_not_loadable(self) -> None:
        with pytest.raises(LiveGuardError, match="GENUS_LIVE_M365=1"):
            load_config({})


# ── 3. the mailbox fence ────────────────────────────────────────────────────


class _Upstream(httpx.AsyncBaseTransport):
    """Records what got past the fence; answers JSON."""

    def __init__(self, body: dict | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.body = body or {"value": []}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json=self.body, request=request)


def _mime(to: str, bcc: str = "") -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = ASSISTANT
    msg["To"] = to
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = TAG
    msg.set_content("hi")
    return base64.b64encode(msg.as_bytes())


async def _send(fence: FencedTransport, method: str, url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=fence) as client:
        return await client.request(method, url, **kwargs)


GRAPH = "https://graph.microsoft.com/v1.0"


class TestFence:
    async def test_assistant_and_owner_pass(self) -> None:
        upstream = _Upstream()
        fence = FencedTransport(upstream, _config())
        await _send(fence, "GET", f"{GRAPH}/users/{ASSISTANT}/messages")
        await _send(fence, "GET", f"{GRAPH}/users/owner%40dev.example/calendar")
        assert len(upstream.seen) == 2

    @pytest.mark.parametrize(
        ("method", "url", "why"),
        [
            ("GET", f"{GRAPH}/users/{STRANGER}/messages", "configured test"),
            ("GET", f"{GRAPH}/me/messages", "only /users"),
            ("GET", f"{GRAPH}/users", "only /users"),
            ("POST", f"{GRAPH}/$batch", "only /users"),
            ("GET", "https://evil.example/v1.0/users/x", "neither Graph nor Entra"),
            ("GET", f"http://graph.microsoft.com/v1.0/users/{ASSISTANT}", "not https"),
            ("POST", f"{GRAPH}/users/{CANARY}/messages", "may only be read"),
            ("DELETE", f"{GRAPH}/users/{CANARY}/messages/x", "may only be read"),
            (
                "POST",
                f"https://login.microsoftonline.com/{OTHER_DIRECTORY}/oauth2/v2.0/token",
                "not allowlisted",
            ),
            (
                "GET",
                f"https://login.microsoftonline.com/{DIRECTORY}/v2.0/.well-known",
                "token endpoint",
            ),
        ],
    )
    async def test_refused_before_sending(self, method, url, why) -> None:
        upstream = _Upstream()
        fence = FencedTransport(upstream, _config())
        with pytest.raises(FenceViolation, match=why):
            await _send(fence, method, url)
        assert upstream.seen == []

    async def test_canary_may_be_read(self) -> None:
        upstream = _Upstream()
        await _send(FencedTransport(upstream, _config()), "GET", f"{GRAPH}/users/{CANARY}/messages")
        assert len(upstream.seen) == 1

    async def test_token_endpoint_for_the_allowlisted_directory_passes(self) -> None:
        upstream = _Upstream()
        url = f"https://login.microsoftonline.com/{DIRECTORY}/oauth2/v2.0/token"
        await _send(FencedTransport(upstream, _config()), "POST", url, data={"a": "b"})
        assert len(upstream.seen) == 1

    @pytest.mark.parametrize("key", ["toRecipients", "ccRecipients", "bccRecipients", "attendees"])
    async def test_json_write_to_an_outsider_is_refused(self, key) -> None:
        upstream = _Upstream()
        body = {
            key: [{"emailAddress": {"address": OWNER}}, {"emailAddress": {"address": STRANGER}}]
        }
        with pytest.raises(FenceViolation, match="recipient"):
            await _send(
                FencedTransport(upstream, _config()),
                "PATCH",
                f"{GRAPH}/users/{ASSISTANT}/messages/x",
                json=body,
            )
        assert upstream.seen == []

    async def test_json_write_to_the_canary_is_refused(self) -> None:
        upstream = _Upstream()
        body = {"attendees": [{"emailAddress": {"address": CANARY}}]}
        with pytest.raises(FenceViolation):
            await _send(
                FencedTransport(upstream, _config()),
                "POST",
                f"{GRAPH}/users/{OWNER}/events",
                json=body,
            )

    async def test_mime_upload_is_checked(self) -> None:
        upstream = _Upstream()
        fence = FencedTransport(upstream, _config())
        url = f"{GRAPH}/users/{ASSISTANT}/messages"
        headers = {"content-type": "text/plain"}
        await _send(fence, "POST", url, content=_mime(OWNER), headers=headers)
        with pytest.raises(FenceViolation):
            await _send(fence, "POST", url, content=_mime(OWNER, bcc=STRANGER), headers=headers)
        assert len(upstream.seen) == 1

    async def test_graph_aliases_are_learned_only_from_graph_links(self) -> None:
        alias = "5a5a5a5a-0000-1111-2222-333333333333"
        upstream = _Upstream(
            {"value": [], "@odata.deltaLink": f"{GRAPH}/users/{alias}/messages/delta?$deltatoken=x"}
        )
        fence = FencedTransport(upstream, _config())
        with pytest.raises(FenceViolation):
            await _send(fence, "GET", f"{GRAPH}/users/{alias}/messages/delta")
        await _send(fence, "GET", f"{GRAPH}/users/{ASSISTANT}/mailFolders/inbox/messages/delta")
        await _send(fence, "GET", f"{GRAPH}/users/{alias}/messages/delta?$deltatoken=x")
        assert fence.aliases == {alias: ASSISTANT}

    async def test_an_alias_learned_from_the_canary_stays_read_only(self) -> None:
        alias = "6b6b6b6b-0000-1111-2222-333333333333"
        upstream = _Upstream(
            {"value": [], "@odata.nextLink": f"{GRAPH}/users/{alias}/messages?p=2"}
        )
        fence = FencedTransport(upstream, _config())
        await _send(fence, "GET", f"{GRAPH}/users/{CANARY}/messages")
        with pytest.raises(FenceViolation, match="may only be read"):
            await _send(fence, "DELETE", f"{GRAPH}/users/{alias}/messages/x")


# ── capture ──────────────────────────────────────────────────────────────────


class _Tenantish(httpx.AsyncBaseTransport):
    """Answers like a tenant would, real-looking data included."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200, json={"access_token": "eyJhbGciOi.eyJ0aWQiOi.c2ln", "expires_in": 3599}
            )
        return httpx.Response(
            200,
            json={
                "id": "AAMkAGI2TG93AAA=",
                "subject": "Quarterly numbers for Jane",
                "from": {"emailAddress": {"name": "Jane Roe", "address": "jane@contoso.com"}},
            },
            headers={
                "request-id": "a1b2c3d4-0000-1111-2222-333344445555",
                "content-type": "application/json",
            },
        )


class TestCapture:
    async def test_capture_writes_scrubbed_records_inside_the_fence(self, tmp_path: Path) -> None:
        scrubber = Scrubber(
            salt=b"s" * 32,
            mailboxes={ASSISTANT: "assistant@tenant.example"},
            forbidden=["contoso.com"],
        )
        capture = CapturingTransport(_Tenantish(), tmp_path, scrubber, label=lambda: "test_x")
        fence = FencedTransport(capture, _config())
        await _send(fence, "GET", f"{GRAPH}/users/{ASSISTANT}/messages/AAMkAGI2TG93AAA%3D")
        with pytest.raises(FenceViolation):
            await _send(fence, "GET", f"{GRAPH}/users/{STRANGER}/messages")
        files = sorted((tmp_path / "test_x").glob("*.json"))
        assert len(files) == 1, "a refused request must never be recorded"
        record = json.loads(files[0].read_text())
        text = files[0].read_text()
        assert "contoso" not in text and "Jane" not in text and "AAMkAGI2" not in text
        assert "request-id" not in text
        assert find_leaks(record) == []
        body_id = record["response"]["body"]["id"]
        assert body_id in record["request"]["url"], "the id in the URL and the body must still join"

    async def test_token_exchange_keeps_no_secret(self, tmp_path: Path) -> None:
        from robothor.workspace.microsoft.auth import ClientCredentialTokenSource
        from robothor.workspace.tests.fake_graph import make_certificate

        cert, key = make_certificate()
        scrubber = Scrubber(salt=b"k" * 32)
        capture = CapturingTransport(_Tenantish(), tmp_path, scrubber, label=lambda: "auth")
        source = ClientCredentialTokenSource(
            DIRECTORY,
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            certificate_pem=cert,
            private_key_pem=key,
            transport=FencedTransport(capture, _config()),
        )
        await source.token()
        record = json.loads(next((tmp_path / "auth").glob("*.json")).read_text())
        body = record["request"]["body"]
        assert body["client_assertion"] == "<jwt>"
        assert body["client_assertion_header"] == {
            "alg": "RS256",
            "typ": "JWT",
            "thumbprints": ["x5t", "x5t#S256"],
        }
        assert record["response"]["body"]["access_token"] == "<redacted>"
        assert DIRECTORY not in json.dumps(record)
        assert find_leaks(record) == []


# ── the scrubber ─────────────────────────────────────────────────────────────


def _scrubber(**kwargs) -> Scrubber:
    kwargs.setdefault("salt", b"salt" * 8)
    return Scrubber(**kwargs)


class TestScrubber:
    def test_requires_a_salt(self) -> None:
        with pytest.raises(ValueError):
            Scrubber(salt=b"")

    def test_configured_mailboxes_become_placeholders(self) -> None:
        s = _scrubber(mailboxes={"robo@contoso.com": "assistant@tenant.example"})
        assert s.text("to ROBO@contoso.com now") == "to assistant@tenant.example now"
        assert s.text("users('robo%40contoso.com')") == "users('assistant%40tenant.example')"

    def test_other_addresses_become_example(self) -> None:
        out = _scrubber().text("mail alice@contoso.com and bob@fabrikam.co.uk")
        assert "contoso" not in out and "fabrikam" not in out and "alice" not in out
        assert out.count(".example") == 2
        assert _scrubber().text("alice@contoso.com") == _scrubber().text("alice@contoso.com")

    def test_guids_and_tokens(self) -> None:
        s = _scrubber()
        out = s.text(f"tenant {DIRECTORY} bearer eyJhbGciOiJSUzI1NiJ9.eyJ0aWQiOiJ4In0.c2lnbmF0dXJl")
        assert DIRECTORY not in out and "eyJ" not in out
        assert "00000000-" in out
        assert s.text("Authorization: Bearer abc.def") == "Authorization: Bearer <redacted>"

    def test_onmicrosoft_names(self) -> None:
        assert "contoso" not in _scrubber().text("contoso.onmicrosoft.com")

    def test_forbidden_literals(self) -> None:
        out = _scrubber(forbidden=["Contoso Ltd"]).text("from contoso ltd hq")
        assert "contoso" not in out.lower()

    def test_ids_hash_consistently_across_url_and_body(self) -> None:
        s = _scrubber()
        url = s.url(f"{GRAPH}/users/{ASSISTANT}/messages/AAMkAGI2TG93AAA%3D/createReplyAll")
        body = s.value({"id": "AAMkAGI2TG93AAA=", "conversationId": "AAQkADAwATM3ZmYA"})
        assert body["id"] in url and url.endswith("/createReplyAll")
        assert body["conversationId"].startswith("id-")

    def test_delta_tokens_are_dropped(self) -> None:
        link = f"{GRAPH}/users/{ASSISTANT}/mailFolders('inbox')/messages/delta?$deltatoken=SECRETDELTA&$skiptoken=SKIP"
        out = _scrubber().url(link)
        assert "SECRETDELTA" not in out and "SKIP" not in out and "%3Ctoken%3E" in out

    def test_etags(self) -> None:
        s = _scrubber()
        tag = s.etag('W/"DwAAABYAAAA="')
        assert tag.startswith('W/"etag-') and s.etag(tag) == tag

    def test_content_kept_only_when_the_suite_wrote_it(self) -> None:
        s = _scrubber()
        mine = s.value(
            {"subject": f"{TAG} reply-all", "body": {"content": f"{TAG} to bob@contoso.com"}}
        )
        assert mine["subject"] == f"{TAG} reply-all"
        assert "contoso" not in mine["body"]["content"] and TAG in mine["body"]["content"]
        theirs = s.value({"subject": "Payroll for March", "bodyPreview": "salaries"})
        assert theirs == {"subject": "<redacted 17 chars>", "bodyPreview": "<redacted 8 chars>"}

    def test_names_follow_the_address(self) -> None:
        s = _scrubber(mailboxes={"robo@contoso.com": "assistant@tenant.example"})
        out = s.value({"emailAddress": {"name": "Robo Assistant", "address": "robo@contoso.com"}})
        assert out == {"emailAddress": {"name": "assistant", "address": "assistant@tenant.example"}}
        assert s.value({"displayName": "Inbox"}) == {"displayName": "Inbox"}
        assert s.value({"location": {"displayName": "Jane's office"}})["location"][
            "displayName"
        ].startswith("<redacted")

    def test_weblinks(self) -> None:
        out = _scrubber().value(
            {"webLink": "https://outlook.office365.com/owa/?ItemID=AAMk&exvsurl=1"}
        )
        assert out == {"webLink": "https://outlook.example/<redacted>"}

    def test_mail_headers_keep_names_and_order(self) -> None:
        s = _scrubber()
        headers = [
            {
                "name": "Received",
                "value": "from BN0PR01MB1234.namprd01.prod.outlook.com (2603:10b6:408:1::1) by mx.contoso.com (10.1.2.3); Tue, 6 Oct 2026 10:00:00 +0000",
            },
            {
                "name": "Authentication-Results",
                "value": "spf=pass (sender IP is 203.0.113.9) smtp.mailfrom=contoso.com; dkim=none (message not signed) header.d=none;dmarc=none action=none header.from=contoso.com;",
            },
            {"name": "Subject", "value": "Salary review"},
            {"name": "Message-ID", "value": "<abc@contoso.com>"},
        ]
        out = s.value({"internetMessageHeaders": headers})["internetMessageHeaders"]
        assert [h["name"] for h in out] == [h["name"] for h in headers]
        text = json.dumps(out)
        assert "contoso" not in text and "10.1.2.3" not in text and "203.0.113.9" not in text
        assert "prod.outlook.com" in text, "Microsoft infrastructure names are kept"
        assert "spf=pass" in text and "dkim=none" in text
        assert out[2]["value"].startswith("<redacted")
        assert find_leaks(out) == []

    def test_scrubbing_is_idempotent(self) -> None:
        s = _scrubber(mailboxes={"robo@contoso.com": "assistant@tenant.example"})
        record = s.exchange(
            method="GET",
            url=f"{GRAPH}/users/robo%40contoso.com/messages/AAMkAGI2TG93AAA%3D?$select=id",
            request_headers={
                "authorization": "Bearer eyJx.eyJy.z",
                "prefer": 'IdType="ImmutableId"',
            },
            request_body=b"",
            status=200,
            response_headers={"content-type": "application/json", "etag": 'W/"CQAAAA=="'},
            response_body=json.dumps(
                {
                    "id": "AAMkAGI2TG93AAA=",
                    "@odata.etag": 'W/"CQAAAA=="',
                    "subject": "x",
                    "toRecipients": [{"emailAddress": {"address": "jo@contoso.com", "name": "Jo"}}],
                }
            ).encode(),
        )
        assert "authorization" not in record["request"]["headers"]
        again = _scrubber(salt=b"another salt").rescrub(record)
        assert again == record

    def test_mime_uploads_are_not_kept(self) -> None:
        body = _scrubber().body(_mime(OWNER), "text/plain", is_token_request=False)
        assert body == {
            "redacted_body": True,
            "content_type": "text/plain",
            "length": len(_mime(OWNER)),
        }


class TestFindLeaks:
    @pytest.mark.parametrize(
        ("value", "kind"),
        [
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc", "JWT"),
            ("Bearer abcdef", "bearer"),
            ("jane@contoso.com", "outside *.example"),
            ("users('jane%40contoso.com')", "outside *.example"),
            (DIRECTORY, "unscrubbed GUID"),
            ("contoso.onmicrosoft.com", "onmicrosoft"),
            ("sender IP is 203.0.113.9", "IP address"),
        ],
    )
    def test_each_kind_is_found_without_echoing_it(self, value: str, kind: str) -> None:
        leaks = find_leaks({"a": [{"b": value}]})
        assert leaks and kind in leaks[0]
        assert value not in leaks[0]
        assert leaks[0].startswith("$.a[0].b")

    def test_forbidden_literal(self) -> None:
        assert find_leaks({"x": "Fabrikam HQ"}, forbidden=["fabrikam"])

    def test_clean(self) -> None:
        clean = {
            "a": "assistant@tenant.example",
            "b": "00000000-1234-5678-9abc-def012345678",
            "c": "192.0.2.4",
            "d": "Bearer <redacted>",
        }
        assert find_leaks(clean) == []


# ── the promotion script ─────────────────────────────────────────────────────


def _load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m365_capture_to_fixtures", REPO / "scripts" / "m365_capture_to_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_capture(directory: Path, test: str, records: list[dict]) -> None:
    folder = directory / test
    folder.mkdir(parents=True, exist_ok=True)
    for seq, record in enumerate(records, 1):
        (folder / f"{seq:05d}.json").write_text(json.dumps({**record, "seq": seq, "test": test}))


def _record(
    scrubber: Scrubber, url: str, body: dict, method: str = "GET", status: int = 200
) -> dict:
    return scrubber.exchange(
        method=method,
        url=url,
        request_headers={},
        request_body=b"",
        status=status,
        response_headers={"content-type": "application/json"},
        response_body=json.dumps(body).encode(),
    )


class TestPromotion:
    def test_promotes_and_replays_into_the_fake_tenant(self, tmp_path: Path) -> None:
        from robothor.workspace.microsoft import graph_client_from_vault
        from robothor.workspace.tests.fake_graph import (
            CLIENT_ID,
            TENANT_ID,
            FakeGraphTenant,
            load_capture_fixture,
            make_certificate,
        )

        script = _load_script()
        first = _scrubber(mailboxes={"robo@contoso.com": "assistant@tenant.example"})
        url = f"{GRAPH}/users/robo%40contoso.com/messages/AAMkAGI2TG93AAA%3D"
        _write_capture(
            tmp_path / "cap",
            "test_reply",
            [
                _record(
                    first, url, {"id": "AAMkAGI2TG93AAA=", "subject": f"{TAG} hi", "isRead": False}
                )
            ],
        )
        written, leaks = script.promote(tmp_path / "cap", tmp_path / "out", forbid=["contoso.com"])
        assert leaks == [] and [p.name for p in written] == ["test_reply.json"]
        exchanges = load_capture_fixture(str(written[0]))
        assert "contoso" not in written[0].read_text()

        cert, key = make_certificate()
        tenant = FakeGraphTenant(certificate_pem=cert)
        tenant.replay(exchanges)
        rows = {
            "workspace/microsoft365/tenant_id": TENANT_ID,
            "workspace/microsoft365/client_id": CLIENT_ID,
            "workspace/microsoft365/client_certificate_pem": cert,
            "workspace/microsoft365/client_private_key_pem": key,
        }

        async def go() -> dict:
            graph = await graph_client_from_vault(
                "t", secret_get=lambda k, tenant_id: rows.get(k), transport=tenant.transport()
            )
            path = httpx.URL(exchanges[0]["request"]["url"]).path.removeprefix("/v1.0")
            async with graph:
                return await graph.get(path)

        import asyncio

        answer = asyncio.run(go())
        assert answer["subject"] == f"{TAG} hi" and answer["id"].startswith("id-")

    def test_a_leak_writes_nothing(self, tmp_path: Path) -> None:
        script = _load_script()
        record = _record(_scrubber(), f"{GRAPH}/users/{ASSISTANT}/messages", {"value": []})
        record["response"]["body"] = {"note": "left behind: jane@contoso.com"}  # a first-pass miss
        record["response"]["headers"] = {"x-leak": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"}
        _write_capture(tmp_path / "cap", "t", [record])
        # The second pass scrubs the address and drops the header. What it
        # cannot rewrite (data in a JSON key) the leak gate still catches.
        record2 = _record(_scrubber(), f"{GRAPH}/users/{ASSISTANT}/messages", {"value": []})
        record2["response"]["body"] = {"jane@contoso.com": "Project Nightjar"}
        _write_capture(tmp_path / "cap2", "t", [record2])
        written, leaks = script.promote(tmp_path / "cap", tmp_path / "out", forbid=[])
        assert leaks == [] and written, "the second pass should have cleaned the first"
        assert "contoso" not in written[0].read_text() and "eyJ" not in written[0].read_text()
        written, leaks = script.promote(tmp_path / "cap2", tmp_path / "out2", forbid=["nightjar"])
        assert written == [] and leaks
        assert all("contoso" not in leak and "Nightjar" not in leak for leak in leaks)
        assert not (tmp_path / "out2").exists()

    def test_main_exit_codes(self, tmp_path: Path, capsys) -> None:
        script = _load_script()
        assert script.main([str(tmp_path / "missing"), str(tmp_path / "out")]) == 1
        (tmp_path / "bad" / "t").mkdir(parents=True)
        (tmp_path / "bad" / "t" / "00001.json").write_text("{not json")
        assert script.main([str(tmp_path / "bad"), str(tmp_path / "out")]) == 1
        _write_capture(
            tmp_path / "ok",
            "t",
            [_record(_scrubber(), f"{GRAPH}/users/{ASSISTANT}/messages", {"value": []})],
        )
        assert script.main([str(tmp_path / "ok"), str(tmp_path / "out"), "--check"]) == 0
        assert not (tmp_path / "out").exists()
        assert script.main([str(tmp_path / "ok"), str(tmp_path / "out")]) == 0
        assert (tmp_path / "out" / "t.json").exists()
        assert script.main([str(tmp_path / "ok"), str(tmp_path / "out2"), "--forbid", "users"]) == 1


# ── waiting ──────────────────────────────────────────────────────────────────


class TestPoll:
    async def test_returns_the_first_truthy_answer(self) -> None:
        answers = iter([None, [], {"id": "x"}])
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        async def probe():
            return next(answers)

        assert await poll(probe, interval=2, sleep=sleep, clock=lambda: 0.0) == {"id": "x"}
        assert slept == [2, 2]

    async def test_times_out_as_an_assertion(self) -> None:
        ticks = iter(range(0, 1000, 10))

        async def sleep(_seconds: float) -> None:
            return None

        async def probe():
            return None

        with pytest.raises(AssertionError, match="waiting for the invitation"):
            await poll(
                probe, timeout=30, what="the invitation", sleep=sleep, clock=lambda: next(ticks)
            )


def test_run_tags_are_unique_and_prefixed() -> None:
    tags = {harness.new_run_tag() for _ in range(50)}
    assert len(tags) == 50
    assert all(t.startswith("genus-live-") for t in tags)
