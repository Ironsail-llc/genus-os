"""The private broker has one owner and does not pass service secrets to browsers."""

import os
import stat

import pytest


def test_service_lease_is_exclusive_and_socket_private(tmp_path):
    from robothor.autonomy.workflows.service import service_socket

    path = tmp_path / "runtime" / "broker.sock"
    with service_socket(path) as listener:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert listener.fileno() >= 0
        with pytest.raises(BlockingIOError), service_socket(path):
            pytest.fail("a second broker must not steal the socket")
    assert not path.exists()
    with service_socket(path):
        assert path.exists()


async def test_driver_starts_without_service_secrets_and_restores_parent(monkeypatch):
    from robothor.autonomy.workflows.service import start_driver

    monkeypatch.setenv("PRIVATE_DATABASE_PASSWORD", "private-fixture")
    monkeypatch.setenv("DEBUG", "pw:*")
    monkeypatch.setenv("PATH", "/fixture/bin")

    class Starter:
        async def start(self):
            assert "PRIVATE_DATABASE_PASSWORD" not in os.environ
            assert "DEBUG" not in os.environ
            assert os.environ["PATH"] == "/fixture/bin"
            raise RuntimeError("fixture-startup-failed")

    with pytest.raises(RuntimeError, match="fixture-startup-failed"):
        await start_driver(Starter())
    assert os.environ["PRIVATE_DATABASE_PASSWORD"] == "private-fixture"
