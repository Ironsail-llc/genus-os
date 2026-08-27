"""A plugin should be able to supply a different sandbox runtime.

`Sandbox` builds its argv around `sandbox_binary()`, which resolves to
rootless podman or docker and nothing else. An instance wanting gVisor,
firejail, or a remote sandbox service had to patch the engine.

Sandboxes are `ctx.sandbox` in DeepSeek Harness, and this is one of the
extension kinds behind the breadth gap.

**This one is opt-in, deliberately, and that is a difference from the
harnesses being matched.** Every other seam here takes effect the moment a
package is installed, which is right for a tool or a model. It is wrong for
the thing that confines untrusted execution: a package that could replace
the sandbox merely by being present could replace it with a no-op, and
nothing would look different. So an installed backend is inert until the
operator names it in `ROBOTHOR_SANDBOX_BACKEND`, and the test that matters
most below is the one asserting installation alone changes nothing.
"""

from __future__ import annotations

import pytest

from robothor.plugins import reload_plugins


def _backend(argv=None):
    def build_argv(*, workspace, run_id, cdp_port=None):
        return list(argv or ["runsc", "run", "--workspace", workspace, run_id])

    return {"build_argv": build_argv}


class _SbxEP:
    group = "genus.sandboxes"

    def __init__(self, backends: dict, name: str = "testsbx"):
        self.name = name
        self._backends = backends

    def load(self):
        return {"genus_contract_version": "1.0", "sandboxes": self._backends}


@pytest.fixture
def install(monkeypatch):
    def _install(backends: dict):
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_SbxEP(backends)])
        reload_plugins()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


class TestTheGroupExists:
    def test_the_loader_declares_it(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.sandboxes") == "sandboxes"

    def test_the_pluginset_carries_it(self, install):
        from robothor.plugins import load_plugins

        install({"gvisor": _backend()})
        assert "gvisor" in (load_plugins(reserved_names=set()).sandboxes or {})


class TestInstallingIsNotSelecting:
    """The security property. Installation alone must change nothing."""

    def test_an_installed_backend_is_inert_until_named(self, install, monkeypatch):
        from robothor.engine.sandbox import active_sandbox_backend

        monkeypatch.delenv("ROBOTHOR_SANDBOX_BACKEND", raising=False)
        install({"gvisor": _backend()})
        assert active_sandbox_backend() is None, (
            "a package changed the sandbox merely by being installed"
        )

    def test_the_argv_is_unchanged_by_a_mere_install(self, install, monkeypatch):
        from robothor.engine.sandbox import Sandbox

        monkeypatch.delenv("ROBOTHOR_SANDBOX_BACKEND", raising=False)
        s = Sandbox(run_id="abc123def456", workspace="/tmp/ws")
        before = s._run_argv("/tmp/ws")
        install({"gvisor": _backend()})
        assert s._run_argv("/tmp/ws") == before

    def test_naming_it_selects_it(self, install, monkeypatch):
        from robothor.engine.sandbox import active_sandbox_backend

        install({"gvisor": _backend()})
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BACKEND", "gvisor")
        assert active_sandbox_backend() is not None

    def test_naming_a_backend_that_is_not_installed_is_loud(self, monkeypatch):
        """Silently falling back to podman would hide a misconfiguration."""
        from robothor.engine.sandbox import active_sandbox_backend

        monkeypatch.setenv("ROBOTHOR_SANDBOX_BACKEND", "not-installed")
        with pytest.raises(RuntimeError, match="not-installed"):
            active_sandbox_backend()


class TestSelectedBackendIsUsed:
    def test_the_plugin_builds_the_argv(self, install, monkeypatch):
        from robothor.engine.sandbox import Sandbox

        install({"gvisor": _backend(["runsc", "run", "--isolated"])})
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BACKEND", "gvisor")
        argv = Sandbox(run_id="abc123def456", workspace="/tmp/ws")._run_argv("/tmp/ws")
        assert argv[0] == "runsc"

    def test_a_backend_without_a_callable_is_refused(self, install, monkeypatch):
        from robothor.engine.sandbox import active_sandbox_backend

        install({"broken": {"binary": "runsc"}})
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BACKEND", "broken")
        with pytest.raises(RuntimeError):
            active_sandbox_backend()

    def test_a_backend_that_raises_does_not_silently_fall_back(self, install, monkeypatch):
        """Falling back to podman on error would be a silent downgrade."""
        from robothor.engine.sandbox import Sandbox

        def _boom(**kw):
            raise RuntimeError("backend exploded")

        install({"bad": {"build_argv": _boom}})
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BACKEND", "bad")
        with pytest.raises(RuntimeError):
            Sandbox(run_id="abc123def456", workspace="/tmp/ws")._run_argv("/tmp/ws")
