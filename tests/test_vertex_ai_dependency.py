"""``vertex_ai/*`` models need google-auth in the core install.

litellm authenticates Vertex AI calls with a Google service account through
``google.oauth2.service_account``, but declares google-auth only inside its
``google`` extra (all of google-cloud-aiplatform). An image built from the core
dependencies therefore failed every ``vertex_ai/*`` call with "Google Cloud SDK
not found" while every other provider worked. The dependency is core now; this
pins that so a future dependency sweep cannot quietly drop it.
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


def test_google_auth_is_a_core_dependency_not_an_extra() -> None:
    core = _core_requirements()
    assert "google-auth" in core, (
        "google-auth must be a core dependency: litellm imports "
        "google.oauth2.service_account for every vertex_ai/* call and only "
        "declares it in its own `google` extra"
    )
    assert core["google-auth"].specifier.contains("2.40.0"), core["google-auth"]


def test_the_service_account_loader_litellm_uses_is_importable() -> None:
    module = importlib.import_module("google.oauth2.service_account")
    assert hasattr(module, "Credentials")
