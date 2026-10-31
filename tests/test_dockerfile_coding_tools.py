"""The runtime image carries what a coding job needs: git, and (opt-in) Claude Code.

A static check of Dockerfile.python: no image is built here. The claude CLI
is pinned like the gws CLI -- a version and a per-architecture sha256 checked
with ``sha256sum -c`` -- and stays out of the image unless the build names a
version, so a build without the checksums never downloads an unverified binary.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile.python").read_text()


def _stage(name: str) -> str:
    match = re.search(rf"^FROM \S+ AS {name}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.M | re.S)
    assert match, f"no stage {name}"
    return match.group(1)


def test_the_production_image_has_git_and_ssh():
    production = _stage("production")
    assert re.search(r"apt-get install[^\n]*(\\\n[^\n]*)*\bgit\b", production)
    assert "openssh-client" in production


def test_the_claude_stage_pins_a_version_and_both_checksums():
    stage = _stage("claude-cli")
    for arg in ("CLAUDE_CODE_VERSION", "CLAUDE_CODE_SHA256_AMD64", "CLAUDE_CODE_SHA256_ARM64"):
        assert re.search(rf"^ARG {arg}=", stage, re.M), arg
    assert "sha256sum -c" in stage
    assert "--version" in stage


def test_the_claude_cli_is_opt_in_by_default():
    stage = _stage("claude-cli")
    assert re.search(r"^ARG CLAUDE_CODE_VERSION=$", stage, re.M)


def test_production_and_dev_copy_the_claude_stage():
    assert "COPY --from=claude-cli" in _stage("production")
    assert "COPY --from=claude-cli" in _stage("dev")
