"""Best-effort fallbacks still leave a trace.

Each of these used to be `except Exception: pass`: the fallback was right, but
nothing anywhere said it had been taken. Each test makes the inner call raise,
checks the fallback value is unchanged, and checks one log record from the
module names what failed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _logged(caplog: pytest.LogCaptureFixture, logger: str, *needles: str) -> bool:
    return any(
        r.name == logger and all(n in r.getMessage() for n in needles) for r in caplog.records
    )


def test_wait_for_ready_logs_each_failed_probe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx

    from robothor import health_contract

    class _Down:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def __aenter__(self) -> _Down:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

        async def get(self, url: str) -> Any:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    with caplog.at_level(logging.DEBUG, logger="robothor.health_contract"):
        ready = asyncio.run(
            health_contract.wait_for_ready("http://svc.invalid", timeout=0.05, backoff=0.01)
        )
    assert ready is False
    assert _logged(caplog, "robothor.health_contract", "http://svc.invalid", "ConnectError")


def test_searxng_url_logs_a_registry_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.rag import web_search

    def _boom(name: str) -> str:
        raise RuntimeError("registry unreadable")

    monkeypatch.delenv("ROBOTHOR_SEARXNG_URL", raising=False)
    monkeypatch.setattr("robothor.services.registry.get_service_url", _boom)
    with caplog.at_level(logging.DEBUG, logger="robothor.rag.web_search"):
        url = web_search._searxng_url()
    assert url == "http://localhost:8888"
    assert _logged(caplog, "robothor.rag.web_search", "registry unreadable")


def test_operator_identity_logs_an_owner_config_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.cli import workflow

    def _boom() -> Any:
        raise ValueError("owner.yaml is not valid YAML")

    monkeypatch.setattr("robothor.owner_config.load_owner_config", _boom)
    monkeypatch.setattr("getpass.getuser", lambda: "alice")
    with caplog.at_level(logging.DEBUG, logger="robothor.cli.workflow"):
        who = workflow._operator_identity()
    assert who == "cli:alice"
    assert _logged(caplog, "robothor.cli.workflow", "owner.yaml is not valid YAML")


def test_find_template_dir_logs_a_resources_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from importlib import resources

    import robothor.setup as setup_mod

    # A package dir with no checkout above it and no bundled scaffold beside it,
    # so resolution reaches the importlib.resources fallback.
    fake_pkg = tmp_path / "site-packages" / "robothor"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(setup_mod, "__file__", str(fake_pkg / "setup.py"))
    monkeypatch.delenv("ROBOTHOR_TEMPLATE_DIR", raising=False)

    def _boom(pkg: str) -> Any:
        raise ModuleNotFoundError("robothor has no files()")

    monkeypatch.setattr(resources, "files", _boom)
    with caplog.at_level(logging.DEBUG, logger="robothor.setup"):
        found = setup_mod._find_template_dir()
    assert found is None
    assert _logged(caplog, "robothor.setup", "robothor has no files()")
