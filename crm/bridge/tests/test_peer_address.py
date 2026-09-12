"""The bridge's idea of "the peer" must be the TCP peer, parseable, and never rewritten from client headers.

Two trust mechanisms for X-Forwarded-For in one process disagree by default:
uvicorn's ProxyHeadersMiddleware trusts loopback, the bridge's own allowlist
does not. The bridge runs uvicorn with proxy headers OFF so only its own logic
decides, and a peer that is not a parseable address is treated as unknown
rather than becoming a limiter key, an audit subject or an INET value.
"""

from __future__ import annotations

import sys
from pathlib import Path

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge_service  # noqa: E402
from routers import auth as auth_router  # noqa: E402


def _request(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": raw_headers,
        "client": (peer, 40000) if peer else None,
    }
    return Request(scope)


def test_uvicorn_runs_with_proxy_headers_off(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "0.0.0.0")
    monkeypatch.setenv("ROBOTHOR_BRIDGE_PORT", "9199")
    options = bridge_service.uvicorn_options()
    assert options["proxy_headers"] is False
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 9199


def test_peer_ip_is_the_tcp_peer_not_a_forwarded_header() -> None:
    request = _request("127.0.0.1", {"X-Forwarded-For": "9.9.9.9", "X-Real-IP": "8.8.8.8"})
    assert auth_router._peer_ip(request) == "127.0.0.1"


def test_unparseable_peer_is_unknown() -> None:
    assert auth_router._peer_ip(_request("not-an-ip")) is None
    assert auth_router._peer_ip(_request(None)) is None


def test_ipv6_peer_is_accepted() -> None:
    assert auth_router._peer_ip(_request("::1")) == "::1"


def test_client_ip_never_comes_from_x_forwarded_for(monkeypatch) -> None:
    from robothor.settings import reset_settings

    monkeypatch.setenv("GENUS_TRUSTED_PROXIES", "127.0.0.1/32")
    reset_settings()
    request = _request("127.0.0.1", {"X-Forwarded-For": "9.9.9.9"})
    assert auth_router._client_ip(request) == "127.0.0.1"
