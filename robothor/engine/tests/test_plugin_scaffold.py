"""``genus plugin new`` scaffolds a plugin repository that builds as-is.

The point of a scaffold is that its first wheel is right: a manifest the
loader accepts, package data that actually lands in the wheel, and an agent
the workspace-asset installer can render. So the test that matters builds the
wheel and reads it back with the platform's own wheel reader.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
import yaml

from robothor.cli.plugins import cmd_plugin
from robothor.plugins import assets, scaffold

if TYPE_CHECKING:
    from pathlib import Path

NAME = "example-agent"
PKG = "example_agent"


def _args(**kw):
    defaults = {"plugin_command": "new", "name": NAME, "dir": None}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_the_layout(tmp_path) -> None:
    root = scaffold.scaffold(NAME, tmp_path / NAME)
    expected = [
        "pyproject.toml",
        "CLAUDE.md",
        "README.md",
        ".gitignore",
        ".github/workflows/ci.yml",
        f"src/{PKG}/__init__.py",
        f"src/{PKG}/genus-plugin.yaml",
        f"src/{PKG}/agents/{NAME}/manifest.template.yaml",
        f"src/{PKG}/agents/{NAME}/instructions.template.md",
        f"src/{PKG}/workflows/.gitkeep",
        "tests/test_plugin.py",
    ]
    for relative in expected:
        assert (root / relative).is_file(), relative


def test_the_manifest_declares_the_agent_and_the_tool(tmp_path) -> None:
    root = scaffold.scaffold(NAME, tmp_path / NAME)
    package = root / "src" / PKG
    manifest = yaml.safe_load((package / "genus-plugin.yaml").read_text())
    assert manifest["name"] == NAME
    assert manifest["agents"] == [f"agents/{NAME}"]
    assert manifest["entry_points"]["genus.tools"] == [PKG]
    declared = assets.parse((package / "genus-plugin.yaml").read_text(), package)
    assert [a.agent_id for a in declared.agents] == [NAME]


def test_the_pyproject_publishes_the_entry_point_and_the_package_data(tmp_path) -> None:
    import tomllib

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["name"] == NAME
    assert project["project"]["entry-points"]["genus.tools"] == {PKG: f"{PKG}:PLUGIN"}
    data = project["tool"]["setuptools"]["package-data"][PKG]
    assert "genus-plugin.yaml" in data
    assert any(p.startswith("agents/") for p in data)
    assert any(p.startswith("workflows/") for p in data)


def test_the_ci_workflow_tests_builds_and_hashes(tmp_path) -> None:
    root = scaffold.scaffold(NAME, tmp_path / NAME)
    workflow = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
    steps = " ".join(
        str(s.get("run", "")) for job in workflow["jobs"].values() for s in job["steps"]
    )
    assert "pytest" in steps
    assert "build" in steps
    assert "sha256sum" in steps


@pytest.mark.parametrize(
    "bad",
    [
        "Example",
        "a_b",
        "-x",
        "x" * 70,
        "../x",
        "",
        "2fa-sync",  # leading digit: `2fa_sync` is not a Python identifier
        "1password",
        "123",
        "class",  # a keyword: `import class` is a SyntaxError
        "import",
        "global",
    ],
)
def test_a_bad_name_is_refused(tmp_path, bad) -> None:
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(bad, tmp_path / "out")


def test_the_refusal_says_what_a_name_must_be(tmp_path) -> None:
    with pytest.raises(scaffold.ScaffoldError) as excinfo:
        scaffold.scaffold("2fa-sync", tmp_path / "out")
    assert "start with a letter" in str(excinfo.value)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("name", ["yes", "no", "on", "off", "true", "null"])
def test_a_name_yaml_would_retype_still_scaffolds_a_valid_declaration(tmp_path, name) -> None:
    """Unquoted, `id: yes` is a boolean and the bundle's id check fails."""
    root = scaffold.scaffold(name, tmp_path / name)
    package = root / "src" / name
    manifest = yaml.safe_load((package / "genus-plugin.yaml").read_text())
    assert manifest["name"] == name
    declared = assets.parse((package / "genus-plugin.yaml").read_text(), package)
    assert [a.agent_id for a in declared.agents] == [name]


def test_a_failed_write_leaves_nothing_behind(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    real = Path.write_text
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    with pytest.raises(OSError):
        scaffold.scaffold(NAME, tmp_path / NAME)
    monkeypatch.setattr(Path, "write_text", real)
    assert not (tmp_path / NAME).exists()
    assert scaffold.scaffold(NAME, tmp_path / NAME).is_dir()  # a re-run just works


def test_a_non_empty_directory_is_refused(tmp_path) -> None:
    target = tmp_path / NAME
    target.mkdir()
    (target / "keep.txt").write_text("the operator's file\n")
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(NAME, target)
    assert (target / "keep.txt").read_text() == "the operator's file\n"


def test_the_cli_verb(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert cmd_plugin(_args()) == 0
    assert (tmp_path / NAME / "pyproject.toml").is_file()
    assert NAME in capsys.readouterr().out
    assert cmd_plugin(_args(dir=str(tmp_path / "elsewhere"))) == 0
    assert (tmp_path / "elsewhere" / "pyproject.toml").is_file()
    assert cmd_plugin(_args()) == 2  # exists now
    assert "Traceback" not in capsys.readouterr().err


def test_new_is_a_real_subcommand() -> None:
    from robothor.cli import _build_parser as build_parser

    args = build_parser().parse_args(["plugin", "new", NAME, "--dir", "/tmp/x"])
    assert args.plugin_command == "new"
    assert args.name == NAME
    assert args.dir == "/tmp/x"


def _build_wheel(project: Path, out: Path) -> Path:
    """Build offline with whichever tool is present, or skip."""
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is not installed, so nothing can build the wheel offline")
    if importlib.util.find_spec("build") is not None:
        command = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out)]
    elif importlib.util.find_spec("pip") is not None:
        command = [
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--no-index", "--disable-pip-version-check", "--wheel-dir", str(out),
        ]  # fmt: skip
    else:
        pytest.skip("neither build nor pip is installed")
    done = subprocess.run(
        [*command, str(project)], capture_output=True, text=True, check=False, cwd=project
    )
    assert done.returncode == 0, done.stdout + done.stderr
    wheels = sorted(out.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_the_scaffold_builds_a_wheel_whose_manifest_declares_the_agent(tmp_path) -> None:
    from robothor.plugins.manifest import parse_manifest
    from robothor.plugins.wheel import open_wheel

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    built = _build_wheel(root, tmp_path / "dist")
    contents = open_wheel(built, tmp_path / "unpacked")

    assert contents.name == NAME
    assert contents.manifest_candidates == (f"{PKG}/genus-plugin.yaml",)
    parsed = parse_manifest(contents.manifest_text)
    assert parsed is not None
    assert parsed.declares("handlers") == {scaffold.tool_name(NAME)}
    assert "genus.tools" in contents.genus_groups()
    declared = assets.declaration_from_wheel(contents)
    assert [a.agent_id for a in declared.agents] == [NAME]


def test_the_built_wheel_passes_the_install_scan(tmp_path) -> None:
    """``review``, not ``blocked``: agent instructions are prompt text and a
    declared agent can carry hooks, which is exactly what review is for.
    Nothing in the scaffold is refused."""
    from robothor.plugins import scan
    from robothor.plugins.wheel import open_wheel

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    contents = open_wheel(_build_wheel(root, tmp_path / "dist"), tmp_path / "unpacked")
    result = scan.scan_wheel(contents)
    assert result.verdict == scan.REVIEW, result.reasons
    # The two expected reasons: prompt text, and a declared agent.
    assert any("prompt text" in reason for reason in result.reasons)
    assert any(NAME in reason and "hook" in reason for reason in result.reasons)
    assert len(result.reasons) == 2, result.reasons


def test_the_scaffold_s_own_tests_pass(tmp_path) -> None:
    import os

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("reserved", ["main", "concierge"])
def test_a_reserved_agent_id_is_refused(tmp_path, reserved) -> None:
    """The platform refuses these at install, so the scaffold must not make one."""
    with pytest.raises(scaffold.ScaffoldError) as excinfo:
        scaffold.scaffold(reserved, tmp_path / reserved)
    assert "reserved" in str(excinfo.value)
    assert not (tmp_path / reserved).exists()


def test_every_directory_the_manifest_documents_is_package_data(tmp_path) -> None:
    import tomllib

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    data = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"
    ][PKG]
    manifest = (root / "src" / PKG / "genus-plugin.yaml").read_text()
    for directory in ("agents/", "workflows/", "patches/", "files/"):
        assert any(p.startswith(directory) for p in data), directory
        assert directory in manifest, directory


def test_a_scaffold_with_a_patch_and_a_file_builds_and_plans(tmp_path) -> None:
    """Uncomment the documented patch and file, build, and the wheel carries
    them and the workspace planner accepts the declaration."""
    from robothor.plugins.wheel import open_wheel

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    package = root / "src" / PKG
    (package / "patches").mkdir()
    (package / "patches" / "main.patch.yaml").write_text(
        "install:\n  set:\n    description: patched\nremove:\n  delete: [description]\n"
    )
    (package / "files").mkdir()
    (package / "files" / "notes.md").write_text("notes\n")
    manifest = package / "genus-plugin.yaml"
    manifest.write_text(
        manifest.read_text()
        + "patches:\n  - agent: main\n    file: patches/main.patch.yaml\n"
        + f"files:\n  - src: files/notes.md\n    dest: brain/{NAME}/notes.md\n    mode: if_missing\n"
    )
    contents = open_wheel(_build_wheel(root, tmp_path / "dist"), tmp_path / "unpacked")
    members = {p.relative_to(contents.root).as_posix() for p in contents.root.rglob("*")}
    assert f"{PKG}/patches/main.patch.yaml" in members
    assert f"{PKG}/files/notes.md" in members

    workspace = tmp_path / "ws"
    (workspace / "docs" / "agents").mkdir(parents=True)
    (workspace / "docs" / "agents" / "main.yaml").write_text("id: main\nname: Main\n")
    declared = assets.declaration_from_wheel(contents)
    lines = assets.plan(declared, NAME, workspace=workspace)
    assert any("docs/agents/main.yaml" in line for line in lines)
    assert any(f"brain/{NAME}/notes.md" in line for line in lines)
