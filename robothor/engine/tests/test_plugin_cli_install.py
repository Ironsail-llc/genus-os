"""``genus plugin install`` and ``genus plugin remove`` at the terminal.

The pipeline and its refusals are pinned in ``test_plugin_installer``. What is
pinned here is the operator's side of it, which is a different set of
properties and has its own ways of going wrong:

* **A refusal is exit 2 and a sentence on stderr**, never a traceback. An
  operator who typed a name wrong, or who hit a blocked wheel, gets told what
  happened and what to do.
* **``--dry-run`` prints the command it would run**, including the pip
  invocation, because the CLI is the one caller that may see a path -- the
  operator already knows where their own box keeps its files, and it is the
  only way to check what would happen without doing it.
* **Every successful verb prints the reload note.** The file changed; the
  running engine has not.
* **``--sha256`` is required for a wheel and the argument parser knows it**, so
  the refusal arrives before anything is read.
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from types import SimpleNamespace

import pytest

from robothor.cli.plugins import cmd_plugin
from robothor.plugins import installer, lockfile

#: The canonical manifest: it declares the contributed tool AND the entry
#: point that carries it, which is what lets the scan compare the wheel's
#: surface to the declaration by NAME rather than only by group.
_MANIFEST = (
    "name: acme-tools\n"
    "contract_version: 1\n"
    "handlers:\n"
    "  - probe\n"
    "entry_points:\n"
    "  genus.tools:\n"
    "    - acme\n"
)
_CODE = 'PLUGIN = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}\n'


def _wheel(path, *, code: str = _CODE) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("acme_tools/__init__.py", code)
        zf.writestr("acme_tools/genus-plugin.yaml", _MANIFEST)
        zf.writestr(
            "acme_tools-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: acme-tools\nVersion: 1.2.3\nSummary: A probe\n",
        )
        zf.writestr(
            "acme_tools-1.2.3.dist-info/entry_points.txt",
            "[genus.tools]\nacme = acme_tools:PLUGIN\n",
        )
    return path.read_bytes()


class _Pip:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, command, **_):
        self.calls.append(list(command))
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A lockfile and a workspace of this test's own, never the operator's."""
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(tmp_path / "plugins.lock"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from robothor.settings import reset_settings

    reset_settings()
    lockfile.forget_warnings()
    yield tmp_path
    reset_settings()


def _args(command, **kw):
    defaults = {
        "name": "",
        "version": None,
        "index": None,
        "accept_review": False,
        "dry_run": False,
        "sha256": None,
        "scan_prompts": False,
        "force": False,
    }
    defaults.update(kw)
    return argparse.Namespace(plugin_command=command, **defaults)


def test_installing_a_local_wheel_prints_the_verdict_and_the_reload_note(
    isolated, capsys, monkeypatch
) -> None:
    path = isolated / "acme_tools-1.2.3-py3-none-any.whl"
    data = _wheel(path)
    pip = _Pip()
    monkeypatch.setattr(installer, "_run_pip", pip)

    code = cmd_plugin(_args("install", name=str(path), sha256=hashlib.sha256(data).hexdigest()))
    out = capsys.readouterr().out
    assert code == 0
    assert "safe" in out
    assert "SIGHUP" in out
    assert pip.calls


def test_a_refusal_is_exit_two_and_a_sentence(isolated, capsys, monkeypatch) -> None:
    path = isolated / "acme_tools-1.2.3-py3-none-any.whl"
    _wheel(path, code="import ctypes\n" + _CODE)
    pip = _Pip()
    monkeypatch.setattr(installer, "_run_pip", pip)

    data = path.read_bytes()
    code = cmd_plugin(_args("install", name=str(path), sha256=hashlib.sha256(data).hexdigest()))
    captured = capsys.readouterr()
    assert code == 2
    assert "ctypes" in captured.err
    assert "Traceback" not in captured.err
    assert pip.calls == []


def test_a_wheel_without_sha256_is_refused_at_the_cli(isolated, capsys) -> None:
    path = isolated / "acme_tools-1.2.3-py3-none-any.whl"
    _wheel(path)
    code = cmd_plugin(_args("install", name=str(path)))
    assert code == 2
    assert "--sha256" in capsys.readouterr().err


def test_dry_run_prints_the_pip_command_and_writes_nothing(isolated, capsys, monkeypatch) -> None:
    path = isolated / "acme_tools-1.2.3-py3-none-any.whl"
    data = _wheel(path)
    pip = _Pip()
    monkeypatch.setattr(installer, "_run_pip", pip)

    code = cmd_plugin(
        _args(
            "install",
            name=str(path),
            sha256=hashlib.sha256(data).hexdigest(),
            dry_run=True,
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "would run" in out
    assert "--no-deps" in out
    assert pip.calls == []
    assert not (isolated / "plugins.lock").exists()


def test_remove_refuses_a_row_this_platform_did_not_install(isolated, capsys) -> None:
    lockfile.write_lockfile(
        {"acme-tools": lockfile.LockRow(name="acme-tools")}, isolated / "plugins.lock"
    )
    code = cmd_plugin(_args("remove", name="acme-tools"))
    assert code == 2
    assert "--force" in capsys.readouterr().err


def test_remove_says_what_it_did_and_to_reload(isolated, capsys, monkeypatch) -> None:
    lockfile.record_install(
        "acme-tools",
        version="1.2.3",
        manifest_sha256="a" * 64,
        kinds=("genus.tools",),
        verdict="safe",
        dist_sha256="b" * 64,
        source=lockfile.LockSource(origin="wheel", installed_at="2026-09-15T00:00:00+00:00"),
        path=isolated / "plugins.lock",
    )
    pip = _Pip()
    monkeypatch.setattr(installer, "_run_pip", pip)
    code = cmd_plugin(_args("remove", name="acme-tools"))
    out = capsys.readouterr().out
    assert code == 0
    assert "SIGHUP" in out
    assert pip.calls[0][-1] == "acme-tools"
    assert lockfile.read_lockfile(isolated / "plugins.lock").row("acme-tools") is None


def test_install_and_remove_are_real_subcommands() -> None:
    """A verb the parser does not know is a verb that never runs. The CLI has
    shipped the other arrangement -- a correct function with no caller."""
    from robothor.cli import _build_parser as build_parser

    parser = build_parser()
    args = parser.parse_args(["plugin", "install", "acme-tools", "--dry-run"])
    assert args.plugin_command == "install"
    assert args.dry_run is True
    args = parser.parse_args(["plugin", "remove", "acme-tools", "--force"])
    assert args.plugin_command == "remove"
    assert args.force is True


def test_install_accepts_the_index_and_review_flags() -> None:
    from robothor.cli import _build_parser as build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "plugin",
            "install",
            "acme-tools==1.2.3",
            "--index",
            "https://example.invalid/index.json",
            "--accept-review",
            "--scan-prompts",
        ]
    )
    assert args.name == "acme-tools==1.2.3"
    assert args.index == "https://example.invalid/index.json"
    assert args.accept_review is True
    assert args.scan_prompts is True
