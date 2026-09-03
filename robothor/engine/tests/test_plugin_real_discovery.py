"""The real entry-point path, not the injected seam.

Every other plugin test monkeypatches ``loader._discover`` with a fake
entry point, and this instance has ZERO plugins installed -- so
``importlib.metadata.entry_points(group=...)`` had never actually loaded a
distribution, in a test or in production. Built, wired, tested, and never
run against the real thing: the shape recorded six times in
``feedback-probe-dont-trust-silence``.

This builds an actual installable distribution on disk -- a real
``.dist-info`` with a real ``entry_points.txt`` -- puts it on ``sys.path``,
and calls the genuine discovery function. If the packaging contract in the
loader's ``_GROUPS`` ever stops matching what a plugin author would write,
this fails and the injected-seam tests do not.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from robothor.plugins import loader


@pytest.fixture
def installed_plugin(tmp_path, monkeypatch):
    """Write a real distribution to disk and put it on sys.path."""
    pkg = tmp_path / "genus_probe_plugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            f'''
            """A minimal but REAL plugin, used to probe the discovery path."""

            CONTRACT = "{loader.CONTRACT_VERSION}"


            def _probe_status() -> str:
                return "ok"


            TOOLS = {{
                "genus_contract_version": CONTRACT,
                "handlers": {{"probe_plugin_status": _probe_status}},
            }}
            '''
        )
    )

    dist = tmp_path / "genus_probe_plugin-1.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: genus-probe-plugin\nVersion: 1.0.0\n"
    )
    (dist / "WHEEL").write_text("Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n")
    (dist / "RECORD").write_text("")
    (dist / "entry_points.txt").write_text("[genus.tools]\nprobe = genus_probe_plugin:TOOLS\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    importlib.invalidate_caches()
    loader.reload_plugins()
    try:
        yield "probe_plugin_status"
    finally:
        # The loader caches discovery in several places and sys.path is only
        # unwound after this teardown, so a plugin installed here would
        # otherwise stay visible to every later test in the session.
        monkeypatch.undo()
        importlib.invalidate_caches()
        sys.modules.pop("genus_probe_plugin", None)
        loader.reload_plugins()


def test_a_real_distribution_is_discovered(installed_plugin):
    """The genuine importlib.metadata path, with nothing monkeypatched."""
    eps = loader._discover()
    names = {getattr(e, "name", "") for e in eps}
    assert "probe" in names, (
        "the real entry-point discovery path found nothing. Every other "
        "plugin test injects _discover, so this is the only check that the "
        "packaging contract a plugin author would actually write still works."
    )


def test_a_real_distribution_loads_end_to_end(installed_plugin):
    """Discovery is not enough -- the payload must survive the loader."""
    result = loader.load_plugins(reserved_names=set())
    assert not result.failures, f"real plugin rejected: {result.failures}"
    assert installed_plugin in result.tools, (
        f"discovered but not exposed; tools={list(result.tools)}"
    )
    assert result.tools[installed_plugin]() == "ok"


def test_the_group_names_match_what_an_author_writes():
    """entry_points.txt section headers are the plugin's public contract."""
    assert "genus.tools" in loader._GROUPS
    for group in loader._GROUPS:
        assert group.startswith("genus."), f"{group!r} is not a namespaced entry-point group"
