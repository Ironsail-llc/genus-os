"""web_fetch must pin the vetted IP WITHOUT throwing the hostname away.

Production defect (2026-09-11): 35 of 57 ``web_fetch`` calls failed in 24h.
The SSRF guard vetted the host, then rewrote the request URL to the vetted IP
literal. CDN-fronted sites answered 403, redirected forever, or failed
certificate verification, because the request the origin saw was addressed to a
bare IP. The guard's intent — never connect to a private/loopback address, and
never let DNS change between the check and the connect — must survive; the
hostname must survive with it.

These tests run a REAL server on loopback and let the real transport dial it, so
they fail if the pin is faked at any layer above the socket. ``allow_loopback``
drops 127/8 from the block list for exactly that reason; every other private
range stays blocked, and one test depends on that.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import socket
import ssl
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from robothor.engine.error_recovery import classify_error
from robothor.engine.models import ErrorType
from robothor.engine.tools.handlers import web
from robothor.engine.tools.handlers.web import _web_fetch

# ─── Local server ────────────────────────────────────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        server: Any = self.server
        server.requests.append(
            {
                "host": self.headers.get("Host"),
                "path": self.path,
                "cookie": self.headers.get("Cookie"),
                "user_agent": self.headers.get("User-Agent"),
            }
        )
        status, headers, body = server.responder(self.path, len(server.requests), self.headers)
        payload = body.encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # silence the test log
        return


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *args: Any) -> None:
        # A deliberately failed TLS handshake is an expected outcome here.
        return


def _self_signed(hostname: str) -> tuple[str, str]:
    """Return (cert_pem_path, key_pem_path) for an ephemeral self-signed cert."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


@pytest.fixture
def local_server():
    """Start throwaway HTTP/HTTPS servers on loopback; stop them afterwards."""
    started: list[_Server] = []

    def _start(responder, *, tls_hostname: str | None = None, host: str = "127.0.0.1") -> Any:
        httpd = _Server((host, 0), _Handler)
        httpd.requests = []  # type: ignore[attr-defined]
        httpd.responder = responder  # type: ignore[attr-defined]
        httpd.sni_names = []  # type: ignore[attr-defined]
        if tls_hostname:
            cert_path, key_path = _self_signed(tls_hostname)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # A bare SSLContext still accepts TLS 1.0/1.1. Even a throwaway test
            # server speaks the protocol floor the platform expects.
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert_path, key_path)
            ctx.sni_callback = lambda _sock, name, _ctx: httpd.sni_names.append(name)  # type: ignore[attr-defined]
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch):
    """Answer for named hosts only; everything else hits the real resolver.

    ``answers[host]`` is a list read one entry per lookup (the last entry
    repeats), so a test can make the SECOND resolution differ from the first —
    which is what a DNS-rebinding attacker does.
    """
    real = socket.getaddrinfo
    state = SimpleNamespace(answers={}, calls={})

    def _stub(host, port=None, *args: Any, **kwargs: Any):
        key = str(host).lower()
        seq = state.answers.get(key)
        if seq is None:
            return real(host, port, *args, **kwargs)
        state.calls[key] = state.calls.get(key, 0) + 1
        ip = seq[min(state.calls[key] - 1, len(seq) - 1)]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _stub)
    return state


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the test's own loopback server be a legal target.

    ONLY 127/8 is dropped. 10/8, 192.168/16, 169.254/16 and friends stay
    blocked — `test_redirect_to_private_target_is_refused` proves it.
    """
    kept = [n for n in web._BLOCKED_NETWORKS if n != ipaddress.ip_network("127.0.0.0/8")]
    monkeypatch.setattr(web, "_BLOCKED_NETWORKS", kept)


def _ok(body: str = "<html><body>hello pinned</body></html>"):
    return lambda path, n, headers: (200, {"Content-Type": "text/html"}, body)


# ─── The request keeps the hostname; the socket goes to the vetted IP ───


class TestHostnamePreserved:
    async def test_host_header_is_the_hostname_while_socket_hits_pinned_ip(
        self, local_server, fake_dns, allow_loopback
    ):
        httpd = local_server(_ok())
        port = httpd.server_address[1]
        # First lookup: the real server. Any later lookup: a decoy that is not
        # listening. Only a pinned connection can succeed.
        fake_dns.answers["pinned.test"] = ["127.0.0.1", "198.51.100.7"]

        result = await _web_fetch({"url": f"http://pinned.test:{port}/page"}, ctx=None)

        assert "error" not in result, result
        assert "hello pinned" in result["content"]
        assert httpd.requests[0]["host"] == f"pinned.test:{port}"
        assert httpd.requests[0]["path"] == "/page"
        # The name was resolved once, for the vetting — never again.
        assert fake_dns.calls["pinned.test"] == 1

    async def test_reported_url_is_the_hostname_url(self, local_server, fake_dns, allow_loopback):
        httpd = local_server(_ok())
        port = httpd.server_address[1]
        fake_dns.answers["pinned.test"] = ["127.0.0.1"]

        result = await _web_fetch({"url": f"http://pinned.test:{port}/"}, ctx=None)

        assert result["url"] == f"http://pinned.test:{port}/"
        assert "127.0.0.1" not in result["url"]


class TestBlockedRanges:
    @pytest.mark.parametrize(
        "url",
        [
            "http://[::]/",  # IPv6 unspecified — the v6 spelling of 0.0.0.0
            "http://[fe80::1]/",  # IPv6 link-local — the v6 metadata neighbourhood
            "http://100.64.0.1/",  # CGNAT shared space: carrier-internal, not public
        ],
    )
    def test_additional_private_ranges_are_blocked(self, url: str) -> None:
        assert web._is_blocked_host(url) is True


class TestPinIsNotOptional:
    async def test_a_response_that_never_dialled_the_pin_is_refused(self, monkeypatch, public_dns):
        """If a transport answers without going through the pinned backend, the
        vetted-IP guarantee is gone even though every check "passed". Fail
        closed rather than trust a response whose socket we cannot account for.
        """
        fake = _FakeClient([_Resp()], dials=False)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: fake.configure(**k))

        result = await _web_fetch({"url": "http://site.test/"}, ctx=None)

        assert "Blocked" in result["error"]
        assert classify_error("web_fetch", result["error"]) is ErrorType.BLOCKED

    async def test_pins_are_keyed_by_host_and_port(self, monkeypatch, public_dns):
        """Two ports on one name are two targets. A host-only key lets a pin
        vetted for :443 authorise a connection to any other port on that name.
        """
        backend = web._PinnedResolutionBackend(delegate=object())
        backend.pin("site.test", 443, "93.184.216.34")

        with pytest.raises(Exception, match="unvetted"):
            await backend.connect_tcp("site.test", 8080)


class TestMalformedRedirect:
    async def test_3xx_without_location_is_an_error(self, local_server, fake_dns, allow_loopback):
        """A 3xx with nowhere to go used to be served to the agent as an empty
        page with status 302 — a silent, contentless success."""
        httpd = local_server(lambda path, n, headers: (302, {}, ""))
        port = httpd.server_address[1]
        fake_dns.answers["pinned.test"] = ["127.0.0.1"]

        result = await _web_fetch({"url": f"http://pinned.test:{port}/"}, ctx=None)

        assert "Location" in result["error"]
        assert "content" not in result


class TestInternationalizedHost:
    async def test_idn_host_is_pinned_under_both_spellings(
        self, local_server, fake_dns, allow_loopback
    ):
        """The guard vets the unicode name; the transport asks for the punycode
        one. Pinning only the spelling the guard saw would refuse every
        internationalized domain as "unvetted"."""
        httpd = local_server(_ok())
        port = httpd.server_address[1]
        fake_dns.answers["xn--bcher-kva.test"] = ["127.0.0.1"]
        fake_dns.answers["bücher.test"] = ["127.0.0.1"]

        result = await _web_fetch({"url": f"http://bücher.test:{port}/"}, ctx=None)

        assert "error" not in result, result
        assert "hello pinned" in result["content"]


class TestUserAgent:
    async def test_request_identifies_itself(self, local_server, fake_dns, allow_loopback):
        """Not every production 403 was the IP rewrite. Measured live on
        2026-09-11: en.wikipedia.org answers 403 to httpx's default
        ``python-httpx/0.28.1`` and 200 to a UA that names the client. Sites
        that block on the library default are a real slice of the failures, so
        web_fetch says who it is.
        """
        httpd = local_server(_ok())
        port = httpd.server_address[1]
        fake_dns.answers["pinned.test"] = ["127.0.0.1"]

        await _web_fetch({"url": f"http://pinned.test:{port}/"}, ctx=None)

        agent = httpd.requests[0]["user_agent"] or ""
        assert agent == web.USER_AGENT
        assert "httpx" not in agent.lower()

    async def test_user_agent_is_operator_overridable(
        self, local_server, fake_dns, allow_loopback, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_WEB_FETCH_USER_AGENT", "CustomFetcher/9.9 (+https://ex.test)")
        httpd = local_server(_ok())
        port = httpd.server_address[1]
        fake_dns.answers["pinned.test"] = ["127.0.0.1"]

        await _web_fetch({"url": f"http://pinned.test:{port}/"}, ctx=None)

        assert httpd.requests[0]["user_agent"] == "CustomFetcher/9.9 (+https://ex.test)"


class TestCookieWalledRedirect:
    async def test_domain_cookie_survives_the_apex_to_www_redirect(
        self, local_server, fake_dns, allow_loopback
    ):
        """Sites that set a cookie on the apex and redirect to www keep
        redirecting until the cookie comes back. The cookie jar keys on the
        request URL's host: rewrite that URL to an IP literal and a
        ``Domain=apex.test`` cookie is rejected outright, so the site bounces
        forever and the agent reports "Too many redirects" — one of the four
        top production errors.
        """
        try:
            www = local_server(
                lambda path, n, headers: (
                    (200, {"Content-Type": "text/html"}, "<html><body>through</body></html>")
                    if "canary=1" in (headers.get("Cookie") or "")
                    else (302, {"Location": f"http://apex.test:{apex.server_address[1]}/"}, "")
                ),
                host="127.0.0.2",
            )
        except OSError as e:  # macOS does not alias the whole 127/8 by default
            pytest.skip(f"second loopback address unavailable: {e}")
        apex = local_server(
            lambda path, n, headers: (
                302,
                {
                    "Set-Cookie": "canary=1; Domain=apex.test; Path=/",
                    "Location": f"http://www.apex.test:{www.server_address[1]}/",
                },
                "",
            ),
            host="127.0.0.1",
        )
        fake_dns.answers["apex.test"] = ["127.0.0.1"]
        fake_dns.answers["www.apex.test"] = ["127.0.0.2"]

        result = await _web_fetch({"url": f"http://apex.test:{apex.server_address[1]}/"}, ctx=None)

        assert "error" not in result, result
        assert "through" in result["content"]
        assert www.requests[0]["cookie"] == "canary=1"


class TestUpstreamErrors:
    async def test_403_names_the_hostname_not_the_ip(self, local_server, fake_dns, allow_loopback):
        httpd = local_server(lambda path, n, headers: (403, {"Content-Type": "text/html"}, "nope"))
        port = httpd.server_address[1]
        fake_dns.answers["cdn.test"] = ["127.0.0.1"]

        result = await _web_fetch({"url": f"http://cdn.test:{port}/search?q=x"}, ctx=None)

        error = result["error"]
        assert "403" in error
        assert f"cdn.test:{port}" in error
        assert "127.0.0.1" not in error


class TestDnsRebinding:
    async def test_rebind_on_a_later_hop_is_refused(self, local_server, fake_dns, allow_loopback):
        """Hop 1 vets public; the redirect re-vets and the name now points at
        a private target. The fetch must refuse, and must never dial it."""
        port_holder: dict[str, int] = {}

        def _responder(path, n, headers):
            return (
                302,
                {"Location": f"http://rebind.test:{port_holder['port']}/next"},
                "",
            )

        httpd = local_server(_responder)
        port_holder["port"] = httpd.server_address[1]
        fake_dns.answers["rebind.test"] = ["127.0.0.1", "10.0.0.5"]

        result = await _web_fetch({"url": f"http://rebind.test:{port_holder['port']}/"}, ctx=None)

        assert "Blocked" in result["error"]
        assert len(httpd.requests) == 1  # the second hop never left the process

    async def test_redirect_to_private_target_is_refused(
        self, local_server, fake_dns, allow_loopback
    ):
        httpd = local_server(
            lambda path, n, headers: (302, {"Location": "http://10.0.0.5/admin"}, "")
        )
        port = httpd.server_address[1]
        fake_dns.answers["pinned.test"] = ["127.0.0.1"]

        result = await _web_fetch({"url": f"http://pinned.test:{port}/"}, ctx=None)

        assert "Blocked" in result["error"]


class TestTls:
    async def test_sni_is_the_hostname_and_untrusted_cert_is_named(
        self, local_server, fake_dns, allow_loopback
    ):
        httpd = local_server(_ok(), tls_hostname="tls.test")
        port = httpd.server_address[1]
        fake_dns.answers["tls.test"] = ["127.0.0.1"]

        result = await _web_fetch({"url": f"https://tls.test:{port}/"}, ctx=None)

        # SNI carried the hostname, not the pinned IP — the handshake proves it
        # even though verification then (correctly) rejects the self-signed cert.
        assert httpd.sni_names == ["tls.test"]
        error = result["error"]
        assert "certificate" in error.lower()
        assert "untrusted" in error.lower()
        assert "tls.test" in error


# ─── Redirect accounting (no network needed) ─────────────────────────


class _Resp:
    def __init__(self, *, status=200, location=None, url=""):
        self.status_code = status
        self.is_redirect = location is not None
        self.headers = {"location": location} if location else {}
        self.text = "<html><body>done</body></html>"
        self.url = url

    def raise_for_status(self):
        return None


class _FakeClient:
    """A client that answers from a script instead of a socket.

    ``dials=True`` marks the transport's backend as having been dialled, which
    is what a real connection does. A fake that skips it is a transport that
    bypassed the pin, and web_fetch must refuse the response.
    """

    def __init__(self, responses, *, dials: bool = True, transport=None, **kwargs):
        self._responses = list(responses)
        self._dials = dials
        self._transport = transport
        self.urls: list[str] = []

    def configure(self, **kwargs):
        """Stand in for ``httpx.AsyncClient(**kwargs)`` — keep the transport."""
        self._transport = kwargs.get("transport", self._transport)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        self.urls.append(str(url))
        if self._dials and self._transport is not None:
            self._transport._pool._network_backend.dialed = True
        if self._responses:
            return self._responses.pop(0)
        return _Resp()


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


class TestRedirectAccounting:
    async def test_canonical_http_to_https_and_www_succeed(self, monkeypatch, public_dns):
        """http→https then apex→www is the web's most common canonical pair.
        It must not eat the redirect budget."""
        fake = _FakeClient(
            [
                _Resp(location="https://site.test/"),
                _Resp(location="https://www.site.test/"),
                _Resp(),
            ]
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: fake.configure(**k))

        result = await _web_fetch({"url": "http://site.test/"}, ctx=None)

        assert "error" not in result, result
        assert result["url"] == "https://www.site.test/"

    async def test_five_real_hops_are_allowed(self, monkeypatch, public_dns):
        fake = _FakeClient([_Resp(location=f"http://site.test/{i}") for i in range(1, 6)])
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: fake.configure(**k))

        result = await _web_fetch({"url": "http://site.test/0"}, ctx=None)

        assert "error" not in result, result

    async def test_six_real_hops_is_too_many(self, monkeypatch, public_dns):
        fake = _FakeClient([_Resp(location=f"http://site.test/{i}") for i in range(1, 8)])
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: fake.configure(**k))

        result = await _web_fetch({"url": "http://site.test/0"}, ctx=None)

        assert "Too many redirects" in result["error"]

    async def test_a_redirect_cycle_stops_immediately(self, monkeypatch, public_dns):
        fake = _FakeClient(
            [
                _Resp(location="http://site.test/b"),
                _Resp(location="http://site.test/a"),
                _Resp(location="http://site.test/b"),
                _Resp(location="http://site.test/a"),
            ]
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: fake.configure(**k))

        result = await _web_fetch({"url": "http://site.test/a"}, ctx=None)

        assert "redirect" in result["error"].lower()
        assert len(fake.urls) <= 4


# ─── error_type: the three failure families must be told apart ───────


class TestErrorClassification:
    def test_ssrf_refusal_classifies_as_blocked(self):
        msg = "Blocked: agents cannot access private/loopback addresses (http://10.0.0.5/)"
        assert classify_error("web_fetch", msg) is ErrorType.BLOCKED

    def test_tls_failure_classifies_as_tls(self):
        msg = (
            "TLS certificate for https://self-signed.test/ is untrusted: "
            "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate"
        )
        assert classify_error("web_fetch", msg) is ErrorType.TLS

    def test_upstream_http_error_is_not_blocked_or_tls(self):
        msg = "Fetch failed: Client error '403 Forbidden' for url 'https://cdn.test/search'"
        assert classify_error("web_fetch", msg) is ErrorType.AUTH
