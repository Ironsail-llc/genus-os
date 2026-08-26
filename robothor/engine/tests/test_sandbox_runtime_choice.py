"""The sandbox picks a runtime the process can actually use, safest first.

`sandbox_binary()` returned `"docker"` unless overridden — while its own
docstring argues the opposite: docker needs a daemon whose socket is
root-equivalent, so handing an agent the docker group defeats the sandbox
that agent is running inside. Rootless podman is a drop-in with the same CLI
and no daemon.

Verified on this instance: the engine user is NOT in the docker group, so
`docker ps` is permission-denied, while rootless podman works. The default
was therefore both the less safe choice AND a non-functional one — an agent
declaring `sandbox: docker` would have failed to start a container at all.
Nothing broke only because every current manifest says `sandbox: host`,
which is to say the sandbox has never actually run here.

So: an explicit setting always wins, and otherwise prefer the rootless
runtime that is present. A security default that cannot execute is not a
security default.
"""

from __future__ import annotations

from robothor.engine.sandbox import sandbox_binary


class TestExplicitAlwaysWins:
    def test_the_env_var_is_honoured(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", "docker")
        assert sandbox_binary() == "docker"

    def test_even_an_unusual_runtime(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", "nerdctl")
        assert sandbox_binary() == "nerdctl"

    def test_whitespace_is_tolerated(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", "  podman  ")
        assert sandbox_binary() == "podman"


class TestTheDefaultPrefersRootless:
    def test_podman_is_chosen_when_present(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SANDBOX_BINARY", raising=False)
        monkeypatch.setattr(
            "robothor.engine.sandbox.shutil.which",
            lambda name: f"/usr/bin/{name}" if name in ("podman", "docker") else None,
        )
        assert sandbox_binary() == "podman", (
            "docker's socket is root-equivalent — the docstring says so itself"
        )

    def test_docker_is_the_fallback_when_podman_is_absent(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SANDBOX_BINARY", raising=False)
        monkeypatch.setattr(
            "robothor.engine.sandbox.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        assert sandbox_binary() == "docker"

    def test_neither_present_still_returns_something_nameable(self, monkeypatch):
        """The caller reports a clear failure; this must not raise here."""
        monkeypatch.delenv("ROBOTHOR_SANDBOX_BINARY", raising=False)
        monkeypatch.setattr("robothor.engine.sandbox.shutil.which", lambda name: None)
        assert sandbox_binary() in ("podman", "docker")

    def test_the_choice_is_stable_within_a_process(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SANDBOX_BINARY", raising=False)
        calls = []

        def which(name):
            calls.append(name)
            return f"/usr/bin/{name}" if name == "podman" else None

        monkeypatch.setattr("robothor.engine.sandbox.shutil.which", which)
        first, second = sandbox_binary(), sandbox_binary()
        assert first == second == "podman"


class TestOnThisInstance:
    def test_the_resolved_runtime_is_executable_by_this_process(self):
        """The point of the change: the default must be one we can run.

        Skips rather than fails where neither runtime is installed — CI has
        no container runtime, and a test that fails there would be noise.
        """
        import shutil

        import pytest

        if not (shutil.which("podman") or shutil.which("docker")):
            pytest.skip("no container runtime installed")
        assert shutil.which(sandbox_binary()), (
            f"sandbox_binary() chose {sandbox_binary()!r}, which is not on PATH"
        )
