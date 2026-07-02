"""ExtensionWatcher + skill-bundle activation (Wave-1 hardening, PR-17).

ExtensionWatcher was built but never instantiated in the daemon; skill bundles
had load_bundles/get_bundle/resolve_slash_command but nothing dispatched them.
This wires the watcher into daemon startup and bundle resolution into the
Telegram text handler.
"""

from __future__ import annotations

import inspect

from robothor.engine.extensions import ExtensionWatcher
from robothor.engine.skill_bundles import resolve_slash_command


def test_watcher_detects_added_file(tmp_path):
    w = ExtensionWatcher(adapter_dir=tmp_path, poll_interval=1)
    assert w._detect_changes() == {}  # empty dir, no changes
    (tmp_path / "new.yaml").write_text("name: x\n")
    changes = w._detect_changes()
    assert any(v == "added" for v in changes.values())


def test_daemon_starts_extension_watcher():
    from robothor.engine import daemon

    src = inspect.getsource(daemon)
    assert "_extension_watcher_loop" in src
    assert 'name="extensions"' in src
    assert "ExtensionWatcher()" in src


def test_bundle_resolution(tmp_path):
    (tmp_path / "release.yaml").write_text(
        "name: release\ndescription: ship it\nskills:\n  - run-tests\n  - tag-version\n"
        "instruction: Follow the release checklist.\n"
    )
    kind, bundle = resolve_slash_command("/release", bundles_dir=tmp_path, skills={})
    assert kind == "bundle"
    assert bundle is not None
    assert bundle.skills == ("run-tests", "tag-version")


def test_unknown_slash_is_not_a_bundle(tmp_path):
    kind, bundle = resolve_slash_command("/nope", bundles_dir=tmp_path, skills={})
    assert kind == "unknown"
    assert bundle is None


def test_telegram_wires_bundle_resolution():
    from robothor.engine import telegram

    src = inspect.getsource(telegram)
    assert "resolve_slash_command(" in src
