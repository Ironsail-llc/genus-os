"""``bedrock/*`` models need boto3 in the core install.

litellm signs every Amazon Bedrock request with AWS credentials resolved by
botocore, and declares boto3 nowhere in its own core dependencies. An image
built from the core dependencies would therefore fail every ``bedrock/*`` call
on import while every other provider worked, the same shape as the
``vertex_ai/*`` failure ``test_vertex_ai_dependency.py`` pins. A developer venv
that happens to carry boto3 for another reason hides it.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[1]


def _core_requirements() -> dict[str, Requirement]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    requirements = (Requirement(spec) for spec in project["dependencies"])
    return {requirement.name.lower(): requirement for requirement in requirements}


def test_boto3_is_a_core_dependency_not_an_accident_of_the_venv() -> None:
    core = _core_requirements()
    assert "boto3" in core, (
        "boto3 must be a core dependency: litellm resolves AWS credentials and "
        "signs every bedrock/* request through botocore, and does not declare it"
    )
    assert core["boto3"].specifier.contains("1.40.0"), core["boto3"]


def test_the_credential_resolver_litellm_uses_is_importable() -> None:
    module = importlib.import_module("botocore.session")
    assert hasattr(module, "Session")
