"""A feature whose dependency vanished should say so, not just stop working.

The MCP server exposes 53 tools, every memory tool among them. On this
instance it had been dead for an unknown period with two independent causes:

  * the client config pointed at a path deleted in the single-repo migration;
  * `robothor mcp` itself crashed with ModuleNotFoundError: No module named
    'mcp'.

The second is the interesting one. `mcp>=1.26,<2` IS declared, in the [mcp]
extra, and [all] includes it — a fresh `pip install -e ".[all]"` gets 1.29.1.
The operator's long-lived venv simply drifted and never did. So the feature
worked perfectly for a new user and was silently absent for the person running
it, and `robothor status` — run many times — never mentioned it.

That is what "high maintenance" actually means here: not fragile design, but
environment drift with no instrument pointed at it. This is the instrument.

Deliberately informational, not alarming. Not every instance wants vision or
federation, so an absent extra is reported as unavailable rather than broken —
what matters is that it is VISIBLE, and that the line names the install command
so the reader can act without going to look it up.
"""

from __future__ import annotations

from robothor.cli.admin import feature_dependency_status


def test_it_reports_every_declared_extra():
    rows = feature_dependency_status()
    names = {r.name for r in rows}
    for expected in ("mcp", "vision", "api", "federation", "tui"):
        assert expected in names, f"{expected!r} extra not reported"
    assert "dev" not in names, "dev tooling is not a runtime feature"
    assert "all" not in names, "'all' is a meta-extra, not a feature"


def test_an_importable_extra_is_available():
    rows = {r.name: r for r in feature_dependency_status()}
    # 'api' backs the engine's own HTTP surface; it is always installed here.
    assert rows["api"].available is True


def test_a_missing_extra_names_the_install_command():
    rows = {r.name: r for r in feature_dependency_status()}
    for row in rows.values():
        if not row.available:
            assert "pip install" in row.hint
            assert row.name in row.hint
            break


def test_the_probe_module_is_not_the_extra_name_by_accident():
    """Import names and distribution names differ (opencv-python -> cv2).

    A check that probes the wrong module reports a working feature as broken
    and trains the reader to ignore the line — the failure mode this exists to
    end.
    """
    rows = {r.name: r for r in feature_dependency_status()}
    assert rows["vision"].probe_module == "cv2"
    assert rows["mcp"].probe_module == "mcp"
    assert rows["tui"].probe_module == "textual"


def test_it_never_raises_even_if_an_import_explodes(monkeypatch):
    """status must print. An optional dependency that raises on import — a
    real case with native wheels on the wrong architecture — must not take the
    whole status command down with it."""
    import robothor.cli.admin as admin

    def boom(_name):
        raise RuntimeError("native library mismatch")

    monkeypatch.setattr(admin.importlib.util, "find_spec", boom)
    rows = feature_dependency_status()
    assert rows and all(r.available is False for r in rows)


def test_status_actually_reports_them():
    """An unwired check is the defect class this exists to catch."""
    import inspect

    from robothor.cli.admin import cmd_status

    assert "feature_dependency_status(" in inspect.getsource(cmd_status)
