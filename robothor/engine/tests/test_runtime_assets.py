"""Boot identity survives neither governance changes nor in-process reinstall."""

from types import SimpleNamespace

import pytest

from robothor.engine.tests.test_source_identity import checkout as checkout
from robothor.tests.test_plugin_installed_integrity import installed as installed


@pytest.mark.parametrize("fault", ["none", "lock", "code", "restored", "reload"])
def test_plugin_boot_identity_refuses_changed_process_assets(installed, monkeypatch, fault):
    from robothor.engine.runtime_assets import PluginBootIdentity

    _, _, lock, dist, root = installed
    monkeypatch.setattr("importlib.metadata.distribution", lambda _: dist)
    identity = PluginBootIdentity.capture(lock)
    if fault == "lock":
        lock.write_text(lock.read_text() + "\n")
    elif fault in {"code", "restored"}:
        path = root / "adapter/__init__.py"
        original = path.read_bytes()
        path.write_text("changed")
        if fault == "restored":
            path.write_bytes(original)
    elif fault == "reload":
        monkeypatch.setattr("robothor.plugins.generation", lambda: -1)
    if fault == "none":
        identity.verify("adapter")
    else:
        with pytest.raises(ValueError):
            identity.verify("adapter")


def test_missing_plugin_cannot_be_adopted_after_boot(tmp_path):
    from robothor.engine.runtime_assets import PluginBootIdentity

    identity = PluginBootIdentity.capture(tmp_path / "missing.lock")
    with pytest.raises(ValueError):
        identity.verify("adapter")


def test_assets_check_the_real_captured_source_identity(checkout):
    from robothor.engine.runtime_assets import RuntimeAssets
    from robothor.engine.source_identity import SourceIdentity

    root, revision = checkout
    assets = RuntimeAssets(SourceIdentity.capture(root), None)
    snapshot = SimpleNamespace(
        platform_revision=revision, metadata=lambda: {"contracts": {"plugins": []}}
    )
    assert assets.verify(snapshot, root)["platform_revision"] == revision
    (root / "robothor/__init__.py").write_text("changed")
    with pytest.raises(ValueError):
        assets.verify(snapshot, root)


@pytest.mark.parametrize("replace_input", [False, True])
def test_assets_verify_installed_service_origin_and_registry_identity(
    checkout, installed, monkeypatch, replace_input
):
    import hashlib
    import importlib.util
    import json
    import zipfile

    from robothor.engine.runtime_assets import PluginBootIdentity, RuntimeAssets
    from robothor.engine.source_identity import SourceIdentity

    source, revision = checkout
    wheel, _, lock, dist, root = installed
    code = b"def factory():\n    return None\nSERVICES = {'services': {'sales.business.example': factory}}\n"
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["adapter/__init__.py"] = code
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    (root / "adapter/__init__.py").write_bytes(code)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    governance = json.loads(lock.read_text())
    governance["plugins"][0]["dist_sha256"] = digest
    lock.write_text(json.dumps(governance))
    spec = importlib.util.spec_from_file_location("test_adapter", root / "adapter/__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dist.entry_points = [SimpleNamespace(group="genus.services", load=lambda: module.SERVICES)]
    monkeypatch.setattr("importlib.metadata.distribution", lambda _: dist)
    monkeypatch.setattr("robothor.engine.services.get_service", lambda _: module.factory)
    assets = RuntimeAssets(SourceIdentity.capture(source), PluginBootIdentity.capture(lock))
    if replace_input:
        from robothor.engine import runtime_assets

        original_open = runtime_assets.open_wheel

        def replace_then_open(path, target):
            wheel.write_bytes(b"replaced after payload verification")
            return original_open(path, target)

        monkeypatch.setattr(runtime_assets, "open_wheel", replace_then_open)
    document = {
        "contracts": {"plugins": [{"name": "adapter", "path": wheel.name}]},
        "files": {wheel.name: {"sha256": digest}},
    }
    snapshot = SimpleNamespace(platform_revision=revision, metadata=lambda: document)
    assert assets.verify(snapshot, wheel.parent)["plugins"][0]["wheel_sha256"] == digest
    monkeypatch.setattr("robothor.engine.services.get_service", lambda _: object())
    with pytest.raises(ValueError):
        assets.verify(snapshot, wheel.parent)
