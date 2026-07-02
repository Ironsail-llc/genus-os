"""SSRF guard tests for web_fetch's host blocking.

Regression for the audit (2026-05-29): the block only checked literal IPs, so a
hostname that resolved to a private/loopback address (DNS rebinding), a redirect
to 127.0.0.1, or 0.0.0.0 all slipped through.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.tools.handlers.web import _is_blocked_host


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://127.0.0.5/",  # anywhere in 127/8
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://foo.internal/",
        "http://bar.local/",
    ],
)
def test_blocks_private_and_loopback(url: str) -> None:
    assert _is_blocked_host(url) is True


def test_allows_public_ip_literal() -> None:
    assert _is_blocked_host("http://93.184.216.34/") is False  # example.com


def test_blocks_hostname_resolving_to_loopback() -> None:
    """DNS-rebinding: an innocent-looking name that resolves to 127.0.0.1."""
    fake = [(2, 1, 6, "", ("127.0.0.1", 0))]  # AF_INET → loopback
    with patch("socket.getaddrinfo", return_value=fake):
        assert _is_blocked_host("http://rebind.evil.example/") is True


def test_blocks_hostname_resolving_to_metadata() -> None:
    fake = [(2, 1, 6, "", ("169.254.169.254", 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        assert _is_blocked_host("http://metadata.evil.example/") is True


def test_allows_hostname_resolving_to_public() -> None:
    fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        assert _is_blocked_host("http://example.com/") is False


def test_unresolvable_host_fails_closed() -> None:
    """An unresolvable name fails CLOSED (blocked) — a name that doesn't resolve
    now could resolve to a private target later (DNS rebinding), so the SSRF
    guard rejects it rather than letting the fetch proceed."""
    with patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
        assert _is_blocked_host("http://does-not-exist.invalid/") is True
