"""Third-party code the engine imports, and whether the operator meant to.

``genus doctor`` asks about the database, the models, the host and the
channels. It asked nothing at all about the one part of the instance that runs
code the platform did not write — so a plugin that stopped loading after an
upgrade was invisible until somebody noticed a capability missing, and a
manifest that changed underneath the engine was invisible full stop.

The severities encode the difference between a fault and a decision:

* ``plugins.lockfile`` is **recommended**. An instance with no lockfile works
  exactly as it always has; the file is what makes disabling possible, not what
  makes the engine run.
* ``plugins.load`` is **required**. A plugin that is installed and refused is a
  capability the operator believes they have. A plugin refused because they
  turned it off is not a fault, and is the one refusal this check accepts.
* ``plugins.drift`` is **required**, and is separate from ``plugins.load`` on
  purpose: both go red for a drifted plugin and they say different things, and
  the one that names ``genus plugin sync`` is the one an operator can act on.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from robothor.doctor.checks import plugins as plugin_checks
from robothor.doctor.tests.conftest import make_ctx
from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}
_MANIFEST = "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n"


class _Dist:
    def __init__(self, name="acme-tools", version="1.0.0", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, name="probe", group="genus.tools", dist=None, payload=None):
        self.name, self.group, self.dist = name, group, dist or _Dist()
        self._payload = payload if payload is not None else _PAYLOAD

    def load(self):
        return self._payload


def _run(check_id, ctx=None):
    check = next(c for c in plugin_checks.CHECKS if c.id == check_id)
    return asyncio.run(check.run(ctx or make_ctx()))


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "plugins.lock"
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(path))
    lockfile.forget_warnings()
    return path


class TestRegistration:
    def test_the_category_is_in_the_doctor(self):
        from robothor.doctor.registry import CHECK_MODULES, builtin_checks

        assert "plugins" in CHECK_MODULES
        ids = {check.id for check in builtin_checks()}
        assert {"plugins.lockfile", "plugins.load", "plugins.drift"} <= ids

    def test_the_severities_say_what_is_a_fault(self):
        by_id = {check.id: check for check in plugin_checks.CHECKS}
        assert by_id["plugins.lockfile"].severity == "recommended"
        assert by_id["plugins.load"].severity == "required"
        assert by_id["plugins.drift"].severity == "required"


class TestLockfileCheck:
    def test_no_lockfile_is_reported_without_being_a_fault(self, lock_path):
        result = _run("plugins.lockfile")
        assert result.status == "fail"
        assert "genus plugin sync" in result.detail

    def test_a_synced_lockfile_passes(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            lockfile.sync()
        result = _run("plugins.lockfile")
        assert result.status == "pass", result.detail

    def test_a_corrupt_lockfile_is_reported(self, lock_path):
        lock_path.write_text("{{{", encoding="utf-8")
        result = _run("plugins.lockfile")
        assert result.status == "fail"
        assert "parse" in result.detail or "JSON" in result.detail

    def test_a_world_readable_lockfile_is_reported(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            lockfile.sync()
        lock_path.chmod(0o644)
        result = _run("plugins.lockfile")
        assert result.status == "fail"
        assert "644" in result.detail


class TestLoadCheck:
    def test_everything_loading_passes(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            result = _run("plugins.load")
        assert result.status == "pass", result.detail

    def test_no_plugins_at_all_passes(self, lock_path):
        with patch.object(loader, "_discover", list):
            result = _run("plugins.load")
        assert result.status == "pass"

    def test_a_deliberately_disabled_plugin_is_not_a_fault(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            lockfile.sync()
            lockfile.set_enabled("acme-tools", False)
            result = _run("plugins.load")
        assert result.status == "pass"
        assert "acme-tools" in result.detail, "a disabled plugin must still be reported"

    def test_a_refused_plugin_names_itself_and_the_reason(self, lock_path):
        broken = _EP(payload={"genus_contract_version": "0.1", "handlers": {"x": 1}})
        with patch.object(loader, "_discover", lambda: [broken]):
            result = _run("plugins.load")
        assert result.status == "fail"
        assert "acme-tools" in result.detail
        assert "contract version" in result.detail


class TestDriftCheck:
    def test_nothing_recorded_is_nothing_to_drift(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            result = _run("plugins.drift")
        assert result.status == "pass"

    def test_an_unchanged_manifest_passes(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            lockfile.sync()
            result = _run("plugins.drift")
        assert result.status == "pass"

    def test_a_swapped_manifest_fails_and_names_the_repair(self, lock_path):
        with patch.object(loader, "_discover", lambda: [_EP()]):
            lockfile.sync()
        swapped = _Dist(manifest=_MANIFEST + "services:\n  - takeover\n")
        with patch.object(loader, "_discover", lambda: [_EP(dist=swapped)]):
            result = _run("plugins.drift")
        assert result.status == "fail"
        assert "acme-tools" in result.detail
        assert "genus plugin sync" in result.detail
