"""Is there a container runtime on this engine at all?

`sandbox_binary()` names a runtime; it does not say whether one exists. With
neither podman nor docker installed it still answers "podman", so the first
anyone learns of the absence is a `Sandbox.start()` failure at step 0 of a run.
`runtime_status()` answers the question up front: which binary, whether it
answers, and if not, why -- in words the run and the doctor can repeat.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from robothor.engine import sandbox as sandbox_mod


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch):
    for name in ("ROBOTHOR_SANDBOX_BINARY", "ROBOTHOR_SANDBOX_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    sandbox_mod.reset_runtime_status_cache()
    yield
    sandbox_mod.reset_runtime_status_cache()


class _Proc:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def test_no_runtime_installed_is_unavailable_and_says_which_to_install(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda _name: None)

    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("nothing to probe when no binary is installed")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", _never)

    status = sandbox_mod.runtime_status()

    assert status.available is False
    assert status.binary == "podman"
    assert "podman" in status.reason and "docker" in status.reason


def test_an_installed_runtime_that_answers_is_available(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    def _run(argv: list[str], **_k: Any) -> _Proc:
        calls.append(argv)
        return _Proc(0)

    monkeypatch.setattr(sandbox_mod.subprocess, "run", _run)

    status = sandbox_mod.runtime_status()

    assert status.available is True
    assert status.binary == "podman"
    assert calls and calls[0][:2] == ["podman", "info"]


def test_an_installed_runtime_that_errors_is_unavailable_with_its_error(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        sandbox_mod.subprocess,
        "run",
        lambda *_a, **_k: _Proc(125, "newuidmap: write to uid_map failed"),
    )

    status = sandbox_mod.runtime_status()

    assert status.available is False
    assert "uid_map" in status.reason


def test_a_runtime_that_hangs_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _hang(argv: list[str], **kwargs: Any) -> Any:
        assert kwargs.get("timeout") is not None, "the probe must be bounded"
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sandbox_mod.subprocess, "run", _hang)

    status = sandbox_mod.runtime_status()

    assert status.available is False
    assert "did not answer" in status.reason


def test_the_answer_is_cached_per_process(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = 0

    def _run(*_a: Any, **_k: Any) -> _Proc:
        nonlocal calls
        calls += 1
        return _Proc(0)

    monkeypatch.setattr(sandbox_mod.subprocess, "run", _run)

    sandbox_mod.runtime_status()
    sandbox_mod.runtime_status()

    assert calls == 1


def test_an_explicit_binary_that_is_not_installed_is_named(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", "nerdctl")
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda _name: None)

    status = sandbox_mod.runtime_status()

    assert status.available is False
    assert status.binary == "nerdctl"
    assert "nerdctl" in status.reason


def test_a_selected_plugin_backend_is_trusted_to_start_its_own_runtime(monkeypatch) -> None:
    """Selecting a backend is the operator's hardening choice. Reporting it
    unavailable would let a fallback route around it, which is the silent
    downgrade the backend seam refuses."""
    monkeypatch.setenv("ROBOTHOR_SANDBOX_BACKEND", "example-backend")
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda _name: None)

    status = sandbox_mod.runtime_status()

    assert status.available is True
    assert "example-backend" in status.binary
