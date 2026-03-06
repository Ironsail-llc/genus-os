"""Tests for _sd_notify() in daemon.py."""

import socket

from robothor.engine.daemon import _sd_notify


class TestSdNotify:
    def test_noop_without_notify_socket(self, monkeypatch):
        """_sd_notify does nothing when NOTIFY_SOCKET is not set."""
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        _sd_notify("READY=1")  # Should not raise

    def test_sends_to_real_socket(self, monkeypatch, tmp_path):
        """_sd_notify sends data to a real Unix datagram socket."""
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
            _sd_notify("WATCHDOG=1")
            data = server.recv(256)
            assert data == b"WATCHDOG=1"
        finally:
            server.close()

    def test_handles_bad_socket_path(self, monkeypatch):
        """_sd_notify handles unreachable socket without raising."""
        monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/path/notify.sock")
        _sd_notify("READY=1")  # Should not raise

    def test_handles_abstract_socket(self, monkeypatch):
        """_sd_notify correctly handles abstract socket addresses (@ prefix)."""
        # Create an abstract socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind("\0test_sd_notify_abstract")
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", "@test_sd_notify_abstract")
            _sd_notify("READY=1")
            data = server.recv(256)
            assert data == b"READY=1"
        finally:
            server.close()
