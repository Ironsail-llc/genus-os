"""The dev overlay puts back exactly what the release file gave up.

Making ``docker-compose.apps.yml`` a release file takes the source bind mounts
and the local ``build:`` sections away from the one person who needs them
daily. The overlay is the whole compensation, so what it restores is not a
matter of taste: it is the complement of what the release file removed, and a
missing entry means a dev loop that silently runs stale code out of an image.

The GPU overlay is here too. It exists because a ``deploy.resources`` GPU
reservation in the base file fails the whole stack on a box with no NVIDIA
runtime, which is most of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
DEV_FILE = INFRA / "docker-compose.dev.yml"
GPU_FILE = INFRA / "docker-compose.gpu.yml"

#: Every source tree the dev loop edits, and where it belongs in a container.
RESTORED_MOUNTS = {
    "engine": ("../robothor:/app/robothor", "../crm:/app/crm"),
    "bridge": ("../robothor:/app/robothor", "../crm:/app/crm"),
    "orchestrator": ("../robothor:/app/robothor", "../crm:/app/crm"),
    "dashboard": ("../app:/app",),
}

#: Services whose image is built locally in the dev loop.
BUILT = ("engine", "dashboard")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.fixture(scope="module")
def dev() -> dict:
    return _load(DEV_FILE)


@pytest.fixture(scope="module")
def dev_services(dev: dict) -> dict:
    return dev.get("services") or {}


class TestTheDevOverlayRestoresTheSourceLoop:
    @pytest.mark.parametrize(("name", "expected"), RESTORED_MOUNTS.items())
    def test_it_mounts_the_trees_the_release_file_dropped(self, dev_services, name, expected):
        mounted = [str(entry) for entry in (dev_services.get(name) or {}).get("volumes") or []]

        for mount in expected:
            assert mount in mounted, f"{name} no longer sees {mount} in the dev loop"

    def test_the_workspace_scaffold_is_mounted_from_the_checkout(self, dev_services):
        engine = [str(entry) for entry in dev_services["engine"]["volumes"]]

        assert "../brain:/workspace/brain" in engine
        assert "../docs:/workspace/docs" in engine

    @pytest.mark.parametrize("name", BUILT)
    def test_it_builds_the_images_locally(self, dev_services, name):
        build = (dev_services.get(name) or {}).get("build") or {}

        assert build.get("context") == ".."
        assert build.get("target") == "dev"
        assert "dockerfile" in build

    @pytest.mark.parametrize("name", ("engine", "bridge", "orchestrator", "migrate"))
    def test_the_python_services_run_the_locally_built_image(self, dev_services, name):
        assert (dev_services.get(name) or {}).get("image") == "genusos/python:dev"

    def test_the_dashboard_runs_the_locally_built_image(self, dev_services):
        assert dev_services["dashboard"]["image"] == "genusos/dashboard:dev"

    def test_it_names_only_services_the_release_file_defines(self, dev_services):
        release = set((_load(INFRA / "docker-compose.apps.yml").get("services") or {}))

        assert set(dev_services) <= release, (
            "an overlay that introduces a service of its own is a second stack, "
            "not an overlay"
        )

    def test_it_restores_no_credential_of_its_own(self):
        text = DEV_FILE.read_text(encoding="utf-8")

        assert "PASSWORD:" not in text
        assert "_KEY:" not in text


class TestTheGpuOverlayIsTheOnlyPlaceGpusAreReserved:
    def test_it_reserves_the_nvidia_devices_for_ollama(self):
        reservations = _load(GPU_FILE)["services"]["ollama"]["deploy"]["resources"][
            "reservations"
        ]
        devices = reservations["devices"]

        assert devices[0]["driver"] == "nvidia"
        assert devices[0]["capabilities"] == ["gpu"]

    def test_it_touches_nothing_but_ollama(self):
        assert list(_load(GPU_FILE)["services"]) == ["ollama"]
