"""Tests for web_fetch SSRF hardening (Wave-1 hardening, PR-4).

Two holes are closed: (1) ``_is_blocked_host`` never resolved hostnames, so a
public name resolving to a private IP (127.0.0.1, the 169.254.169.254 metadata
IP) slipped through; (2) ``follow_redirects=True`` followed a redirect to a
private target without re-checking. Now we resolve + check all IPs (fail-closed)
and re-validate every redirect hop.
"""

from __future__ import annotations

import socket

import httpx

from robothor.engine.tools.handlers.web import (
    _is_blocked_host,
    _resolve_and_vet,
    _web_fetch,
)


def _fake_getaddrinfo(ip):
    return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


class TestIsBlockedHost:
    def test_blocks_hostname_resolving_to_loopback(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
        assert _is_blocked_host("http://evil.example/") is True

    def test_blocks_hostname_resolving_to_metadata_ip(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
        assert _is_blocked_host("http://metadata.evil/") is True

    def test_allows_public_host(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
        assert _is_blocked_host("http://example.com/") is False

    def test_blocks_private_ip_literal_without_dns(self):
        assert _is_blocked_host("http://10.0.0.5/") is True
        assert _is_blocked_host("http://127.0.0.1:6379/") is True

    def test_blocks_localhost(self):
        assert _is_blocked_host("http://localhost:8080/") is True

    def test_unresolvable_host_fails_closed(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("nxdomain")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        assert _is_blocked_host("http://nope.invalid/") is True


class _FakeResp:
    def __init__(self, *, is_redirect=False, location=None):
        self.is_redirect = is_redirect
        self.headers = {"location": location} if location else {}
        self.status_code = 302 if is_redirect else 200
        self.text = "<html><body>ok</body></html>"
        self.url = "http://example.com/"

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        self.calls.append({"url": str(url), **kwargs})
        return self._responses.pop(0)


class TestResolveAndVet:
    def test_returns_vetted_public_ip(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
        blocked, ip = _resolve_and_vet("http://example.com/")
        assert blocked is False
        assert ip == "93.184.216.34"

    def test_blocks_and_returns_no_ip_for_private(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
        blocked, ip = _resolve_and_vet("http://evil.example/")
        assert blocked is True
        assert ip is None

    def test_ip_literal_pins_itself(self):
        blocked, ip = _resolve_and_vet("http://93.184.216.34/")
        assert blocked is False
        assert ip == "93.184.216.34"


class TestWebFetchRedirects:
    async def test_blocks_redirect_to_loopback(self, monkeypatch):
        # Public host resolves fine; the redirect points at loopback.
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **k: _FakeClient(
                [_FakeResp(is_redirect=True, location="http://127.0.0.1:6379/")]
            ),
        )
        result = await _web_fetch({"url": "http://example.com/"}, ctx=None)
        assert "Blocked" in result.get("error", "")

    async def test_does_not_auto_follow_redirects(self, monkeypatch):
        """The client must be created with follow_redirects=False."""
        captured = {}
        real = httpx.AsyncClient

        def _spy(**kwargs):
            captured.update(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _spy)
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
        # loopback resolution short-circuits before any network call
        await _web_fetch({"url": "http://blocked.example/"}, ctx=None)
        assert captured.get("follow_redirects") is False

    async def test_pins_vetted_ip_against_dns_rebinding(self, monkeypatch):
        """The fetch must connect to the IP vetted at check time, not re-resolve.

        A DNS-rebinding attacker returns a public IP on the first (validation)
        lookup, then a private IP on the second (connection) lookup. Because we
        pin the vetted IP onto the request, a second resolution never happens and
        the private target is never reached.
        """
        calls = {"n": 0}

        def _flip(*a, **k):
            calls["n"] += 1
            ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _flip)
        fake = _FakeClient([_FakeResp()])
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: fake)

        result = await _web_fetch({"url": "http://example.com/"}, ctx=None)

        assert "error" not in result
        assert len(fake.calls) == 1
        call = fake.calls[0]
        # Connected to the pinned public IP, carrying the real Host + SNI.
        assert "93.184.216.34" in call["url"]
        assert call["headers"]["Host"] == "example.com"
        assert call["extensions"]["sni_hostname"] == "example.com"
