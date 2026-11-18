"""The released python image carries a pinned, checksummed Claude Code CLI.

Dockerfile.python installs the CLI only when the build names a version and a
sha256 per architecture. These tests pin down that the release workflow does
name them, from one place (the workflow's top-level ``env``), and that what it
names is shaped like a real pin: an exact version and two 64-hex digests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-and-build.yml"
PIN_KEYS = ("CLAUDE_CODE_VERSION", "CLAUDE_CODE_SHA256_AMD64", "CLAUDE_CODE_SHA256_ARM64")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _build_step(workflow: dict) -> dict:
    steps = workflow["jobs"]["build"]["steps"]
    return next(s for s in steps if s.get("id") == "build")


def test_the_pin_lives_in_the_workflow_env(workflow: dict) -> None:
    env = workflow.get("env") or {}
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(env.get("CLAUDE_CODE_VERSION", "")))
    for key in ("CLAUDE_CODE_SHA256_AMD64", "CLAUDE_CODE_SHA256_ARM64"):
        assert re.fullmatch(r"[0-9a-f]{64}", str(env.get(key, ""))), key


def test_the_python_build_passes_every_pin_as_a_build_arg(workflow: dict) -> None:
    build_args = str(_build_step(workflow)["with"].get("build-args", ""))
    lines = [line for line in build_args.splitlines() if line.strip()]
    for key in PIN_KEYS:
        assert any(f"{key}=" in line and f"env.{key}" in line for line in lines), key


def test_only_the_python_image_gets_the_pin(workflow: dict) -> None:
    # Dockerfile.app has no claude-cli stage; the args are gated on the matrix.
    build_args = str(_build_step(workflow)["with"].get("build-args", ""))
    assert "matrix.image.name == 'python'" in build_args
