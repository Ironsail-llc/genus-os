"""``infra/docker-compose.apps.yml`` is a RELEASE file, not a dev file.

It used to build ``genusos/python:dev`` from a source checkout and bind-mount
``../robothor``, ``../crm`` and ``../app`` over the image, so the enterprise
pilot path — ``docker compose up`` on a fresh box — could not work at all:
there is no source tree to mount, and nothing ever created the schema, so four
services came up and 503'd on ``/ready`` forever.

These assertions are the shape of that fix, and every one of them is a defect
that was invisible on a developer's machine (where the mounts happen to
resolve) and fatal on the operator's.

The ports are read from the settings model rather than typed here. A
healthcheck probing a port the platform no longer serves reports a service
down that is fine, or — worse, and this is what shipped — a dashboard
published on 3000 while every printed first-run link says 3004.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from robothor.settings.model import AuthSettings, EngineSettings, ServiceSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
APPS_FILE = INFRA / "docker-compose.apps.yml"
BASE_FILE = INFRA / "docker-compose.yml"

#: Every service the platform itself ships in the release file.
PLATFORM_SERVICES = ("migrate", "engine", "bridge", "orchestrator", "dashboard")

#: The three long-running Python services. They are the ones that must not
#: start before the schema exists.
PYTHON_SERVICES = ("engine", "bridge", "orchestrator")

#: The registry every released image comes from. There is no `:latest`.
GHCR_PREFIX = "ghcr.io/ironsail-llc/genus-os/"

#: Source trees whose bind mount turns a release image into a dev container.
SOURCE_MOUNTS = ("../robothor", "../crm", "../app")

#: Anything shaped like a credential pasted into a tracked file.
SECRET_SHAPED = re.compile(r"(_KEY|TOKEN|SECRET|PASSWORD)\s*[:=]\s*(?!\$|\"?\$)\S")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.fixture(scope="module")
def apps() -> dict:
    return _load(APPS_FILE)


@pytest.fixture(scope="module")
def services(apps: dict) -> dict:
    return apps.get("services") or {}


def _mount_sources(service: dict) -> list[str]:
    """The host side of every bind mount on one service."""
    sources = []
    for entry in service.get("volumes") or []:
        if isinstance(entry, str):
            sources.append(entry.split(":", 1)[0])
        elif isinstance(entry, dict):
            sources.append(str(entry.get("source", "")))
    return sources


def _healthcheck_test(service: dict) -> str:
    test = (service.get("healthcheck") or {}).get("test") or []
    return " ".join(test) if isinstance(test, list) else str(test)


class TestItShipsReleasedImages:
    def test_no_platform_service_builds_from_source(self, services):
        building = [name for name in PLATFORM_SERVICES if "build" in (services.get(name) or {})]

        assert building == [], (
            f"{building} build from a source checkout; the release file runs "
            "published images and infra/docker-compose.dev.yml restores the builds"
        )

    @pytest.mark.parametrize("name", PLATFORM_SERVICES)
    def test_every_image_comes_from_the_release_registry(self, services, name):
        image = str((services.get(name) or {}).get("image", ""))

        assert image.startswith(GHCR_PREFIX), f"{name} runs {image!r}, not a {GHCR_PREFIX} image"

    @pytest.mark.parametrize("name", (*PYTHON_SERVICES, "migrate"))
    def test_the_python_services_share_one_image(self, services, name):
        image = str((services.get(name) or {}).get("image", ""))

        assert image.startswith(f"{GHCR_PREFIX}python:")

    def test_the_dashboard_runs_the_app_image(self, services):
        assert str(services["dashboard"]["image"]).startswith(f"{GHCR_PREFIX}app:")

    @pytest.mark.parametrize("name", PLATFORM_SERVICES)
    def test_the_tag_is_a_variable_the_operator_must_set(self, services, name):
        image = str((services.get(name) or {}).get("image", ""))

        # `:?` rather than a default. The release workflow publishes vX.Y.Z,
        # vX.Y, vX and sha-* and NO floating `latest`, so a default would name
        # a tag that does not exist and fail at pull time with "manifest
        # unknown" instead of here, naming the variable.
        assert ":${GENUS_IMAGE_TAG:?" in image, (
            f"{name} pins {image!r}; use ${{GENUS_IMAGE_TAG:?...}} so a missing "
            "tag fails with a sentence rather than a registry 404"
        )


class TestNoSourceTreeIsMountedOverTheImage:
    @pytest.mark.parametrize("name", PLATFORM_SERVICES)
    def test_the_platform_source_is_not_bind_mounted(self, services, name):
        mounted = [
            source
            for source in _mount_sources(services.get(name) or {})
            if any(source.startswith(tree) for tree in SOURCE_MOUNTS)
        ]

        assert mounted == [], f"{name} mounts {mounted} over the released image"

    def test_instance_data_arrives_through_one_workspace_mount(self, services):
        for name in (*PYTHON_SERVICES, "migrate"):
            sources = _mount_sources(services[name])
            workspace = [source for source in sources if "GENUS_WORKSPACE" in source]

            assert len(workspace) == 1, (
                f"{name} should take the instance's workspace as exactly one "
                f"mount, not {sources}"
            )


class TestNothingStartsBeforeTheSchemaExists:
    def test_there_is_a_one_shot_migrate_service(self, services):
        migrate = services.get("migrate") or {}

        assert migrate.get("command") == ["python", "-m", "robothor.cli", "migrate"]
        # A restarting migration container is an infinite loop that never
        # satisfies `service_completed_successfully`, so the three services
        # waiting on it wait forever.
        assert migrate.get("restart") == "no"
        assert (migrate.get("depends_on") or {}).get("postgres") == {
            "condition": "service_healthy"
        }

    @pytest.mark.parametrize("name", PYTHON_SERVICES)
    def test_the_python_services_wait_for_it_to_finish(self, services, name):
        depends = (services.get(name) or {}).get("depends_on") or {}

        assert depends.get("migrate") == {"condition": "service_completed_successfully"}, (
            f"{name} starts without waiting for the migration to complete; on a "
            "fresh box that is a service that 503s on /ready forever"
        )

    def test_the_dashboard_waits_for_the_bridge(self, services):
        depends = services["dashboard"]["depends_on"]

        assert depends.get("bridge") == {"condition": "service_healthy"}


class TestTheHealthchecksProbeThePortsThePlatformServes:
    @pytest.mark.parametrize(
        ("name", "port", "path"),
        [
            ("engine", EngineSettings().port, "/ready"),
            ("bridge", AuthSettings().bridge_port, "/ready"),
            ("orchestrator", ServiceSettings().orchestrator_port, "/ready"),
            ("dashboard", ServiceSettings().helm_port, "/api/ready"),
        ],
    )
    def test_each_service_is_probed_where_it_listens(self, services, name, port, path):
        probe = _healthcheck_test(services[name])

        assert f"localhost:{port}{path}" in probe, (
            f"{name}'s healthcheck is {probe!r}, which is not the "
            f"{port}{path} the settings model declares"
        )

    @pytest.mark.parametrize("name", ("engine", "bridge", "orchestrator", "dashboard"))
    def test_each_service_publishes_the_port_it_serves(self, services, name):
        published = [str(entry) for entry in services[name].get("ports") or []]

        assert published, f"{name} publishes no port, so nothing on the host can reach it"
        for entry in published:
            host, container = entry.rsplit(":", 1)
            assert host.endswith(container), (
                f"{name} maps {entry}: a host port that differs from the container "
                "port makes every printed URL wrong"
            )


class TestSecretsComeFromTheEnvFileAndNowhereElse:
    @pytest.mark.parametrize("name", PLATFORM_SERVICES)
    def test_every_service_reads_the_env_file_genus_init_writes(self, services, name):
        entries = (services.get(name) or {}).get("env_file") or []
        rendered = [
            entry if isinstance(entry, str) else str(entry.get("path", "")) for entry in entries
        ]

        assert any("GENUS_ENV_FILE" in entry for entry in rendered), (
            f"{name} does not read ${{GENUS_ENV_FILE}}; its credentials would "
            "have to be inlined here"
        )

    def test_the_file_holds_no_credential_of_its_own(self):
        offenders = [
            line.strip()
            for line in APPS_FILE.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#") and SECRET_SHAPED.search(line)
        ]

        assert offenders == [], f"secret-shaped literals in the release file: {offenders}"

    def test_the_containers_default_to_the_env_secrets_backend(self, services):
        for name in (*PYTHON_SERVICES, "migrate"):
            environment = services[name].get("environment") or {}

            assert environment.get("ROBOTHOR_SECRETS_BACKEND") == "env", (
                f"{name} must read credentials from its environment: the "
                "load-secrets.sh/systemd path writes /run/robothor/secrets.env, "
                "which no container has"
            )

    def test_no_stale_telegram_note_survives(self):
        text = APPS_FILE.read_text(encoding="utf-8")

        assert "aiogram" not in text
        assert "this session's stash" not in text


class TestTheBaseFileCreatesNoSchemaOfItsOwn:
    def test_it_mounts_no_migrations(self):
        text = BASE_FILE.read_text(encoding="utf-8")
        mounts = [
            line.strip()
            for line in text.splitlines()
            if "migrations" in line and not line.lstrip().startswith("#")
        ]

        assert mounts == [], (
            f"{mounts}: SQL mounted into docker-entrypoint-initdb.d runs outside "
            "the schema_migrations_v2 ledger, and a ledger-less schema is one no "
            "upgrade can tell from an empty database"
        )

    def test_it_still_carries_the_three_infrastructure_services(self):
        services = (_load(BASE_FILE).get("services") or {}).keys()

        assert {"postgres", "redis", "ollama"} <= set(services)

    def test_the_gpu_reservation_moved_to_its_own_overlay(self):
        ollama = _load(BASE_FILE)["services"]["ollama"]

        # A GPU reservation in the base file makes `docker compose up` fail
        # outright on every box without the NVIDIA runtime -- which is most
        # enterprise pilot boxes. The wizard adds the overlay when nvidia-smi
        # answers.
        assert "deploy" not in ollama
