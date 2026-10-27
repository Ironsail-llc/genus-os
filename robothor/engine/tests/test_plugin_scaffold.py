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


@pytest.mark.parametrize("bad", ["Example", "a_b", "-x", "x" * 70, "../x", ""])
def test_a_bad_name_is_refused(tmp_path, bad) -> None:
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(bad, tmp_path / "out")


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
    """``review``, not ``blocked``: agent instructions are prompt text, which
    is exactly what review is for. Nothing in the scaffold is refused."""
    from robothor.plugins import scan
    from robothor.plugins.wheel import open_wheel

    root = scaffold.scaffold(NAME, tmp_path / NAME)
    contents = open_wheel(_build_wheel(root, tmp_path / "dist"), tmp_path / "unpacked")
    result = scan.scan_wheel(contents)
    assert result.verdict == scan.REVIEW, result.reasons
    assert all("prompt text" in reason for reason in result.reasons)


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
