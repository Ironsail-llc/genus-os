"""``bedrock/*`` models need boto3, and the platform says so itself.

litellm signs every Amazon Bedrock request with AWS credentials resolved by
botocore. litellm 1.100 lists boto3 among its own dependencies, so the install
carries it today. It keeps google-auth, which Vertex AI needs the same way, in
an extra instead, and ``test_vertex_ai_dependency.py`` records what that cost.
Declaring boto3 in the core dependencies means a litellm release that moves it
cannot take Bedrock away from an instance that runs on it.
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


def test_boto3_is_a_core_dependency_in_its_own_right() -> None:
    core = _core_requirements()
    assert "boto3" in core, (
        "boto3 must be a core dependency: litellm resolves AWS credentials and "
        "signs every bedrock/* request through botocore, and the platform should "
        "not depend on litellm continuing to declare it"
    )
    assert core["boto3"].specifier.contains("1.40.0"), core["boto3"]


def test_the_credential_resolver_litellm_uses_is_importable() -> None:
    module = importlib.import_module("botocore.session")
    assert hasattr(module, "Session")
