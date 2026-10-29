"""Tenant engine cells must not change a release that declares none.

The chart renders one extra engine per entry in ``tenants:``. An existing
release upgrades with ``tenants: []``, and for it the set of objects Kubernetes
sees must be exactly what it saw before cells existed: a renamed or extra
object there is a deletion or a surprise in a live namespace. The baseline
below is the ``helm template`` output of the chart as it stood before tenant
cells, reduced to (kind, name) pairs.

The per-field behaviour of a cell lives in ``helm/genus-os/tests/tenants_test.yaml``
(helm-unittest); this file holds the whole-release checks that helm-unittest,
which renders one template at a time, cannot express.

Skipped where ``helm`` or the chart's vendored dependencies are missing: CI's
Helm job builds them, the Python job does not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART = REPO_ROOT / "helm" / "genus-os"

pytestmark = [
    pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed"),
    pytest.mark.skipif(
        not list((CHART / "charts").glob("*.tgz")),
        reason="chart dependencies not built (helm dependency build helm/genus-os)",
    ),
]

#: `helm template gos helm/genus-os -n genus` before tenant cells existed.
#: The migration Job's hash suffix is normalised away: it tracks the migration
#: pod template, not the set of objects.
BASELINE_DEFAULT = {
    ("ConfigMap", "gos-postgres-scripts"),
    ("ConfigMap", "gos-valkey-config"),
    ("ConfigMap", "gos-valkey-init-scripts"),
    ("Deployment", "gos-genus-os-bridge"),
    ("Deployment", "gos-genus-os-dashboard"),
    ("Deployment", "gos-genus-os-engine"),
    ("Deployment", "gos-genus-os-orchestrator"),
    ("Deployment", "gos-valkey"),
    ("Job", "gos-genus-os-migrate-*"),
    ("PersistentVolumeClaim", "gos-valkey"),
    ("Pod", "gos-genus-os-helm-test"),
    ("Pod", "gos-valkey-test-auth-existing"),
    ("PodDisruptionBudget", "gos-genus-os-bridge"),
    ("PodDisruptionBudget", "gos-genus-os-dashboard"),
    ("PodDisruptionBudget", "gos-genus-os-orchestrator"),
    ("Role", "gos-genus-os-migration-watcher"),
    ("RoleBinding", "gos-genus-os-migration-watcher"),
    ("Secret", "gos-postgres"),
    ("Service", "gos-genus-os-bridge"),
    ("Service", "gos-genus-os-dashboard"),
    ("Service", "gos-genus-os-engine"),
    ("Service", "gos-genus-os-orchestrator"),
    ("Service", "gos-postgres"),
    ("Service", "gos-valkey"),
    ("ServiceAccount", "gos-genus-os-bridge"),
    ("ServiceAccount", "gos-genus-os-dashboard"),
    ("ServiceAccount", "gos-genus-os-engine"),
    ("ServiceAccount", "gos-genus-os-helm-test"),
    ("ServiceAccount", "gos-genus-os-migrations"),
    ("ServiceAccount", "gos-genus-os-orchestrator"),
    ("ServiceAccount", "gos-valkey"),
    ("StatefulSet", "gos-postgres"),
    ("VaultStaticSecret", "gos-genus-os-auth-signing"),
    ("VaultStaticSecret", "gos-genus-os-bridge-integrations"),
    ("VaultStaticSecret", "gos-genus-os-bridge-oidc"),
    ("VaultStaticSecret", "gos-genus-os-bridge-sso"),
    ("VaultStaticSecret", "gos-genus-os-cache"),
    ("VaultStaticSecret", "gos-genus-os-dashboard-auth"),
    ("VaultStaticSecret", "gos-genus-os-database"),
    ("VaultStaticSecret", "gos-genus-os-engine-providers"),
    ("VaultStaticSecret", "gos-genus-os-orchestrator-providers"),
}

TWO_TENANTS = [
    "--set",
    "workspace.persistence.enabled=true",
    "--set",
    "tenants[0].id=acme-child-a",
    "--set",
    "tenants[1].id=acme-child-b",
]


def _render(*args: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "gos", str(CHART), "--namespace", "genus", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _kind_names(docs: list[dict]) -> set[tuple[str, str]]:
    pairs = set()
    for doc in docs:
        name = doc["metadata"]["name"]
        if doc["kind"] == "Job" and name.startswith("gos-genus-os-migrate-"):
            name = "gos-genus-os-migrate-*"
        pairs.add((doc["kind"], name))
    return pairs


def _env(doc: dict) -> dict[str, str]:
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_values_declare_an_empty_tenant_list() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["tenants"] == []
    assert values["global"]["rootTenant"] == "default"


def test_empty_tenants_render_the_same_objects_as_before() -> None:
    assert _kind_names(_render()) == BASELINE_DEFAULT


def test_two_tenants_add_exactly_their_cells() -> None:
    before = _kind_names(_render("--set", "workspace.persistence.enabled=true"))
    after = _kind_names(_render(*TWO_TENANTS))
    assert after - before == {
        ("Deployment", "gos-genus-os-engine-acme-child-a"),
        ("Service", "gos-genus-os-engine-acme-child-a"),
        ("PersistentVolumeClaim", "gos-genus-os-workspace-acme-child-a"),
        ("Deployment", "gos-genus-os-engine-acme-child-b"),
        ("Service", "gos-genus-os-engine-acme-child-b"),
        ("PersistentVolumeClaim", "gos-genus-os-workspace-acme-child-b"),
    }
    assert before - after == set()


def test_every_engine_pod_agrees_with_itself_about_its_tenant() -> None:
    engines = [
        d
        for d in _render(*TWO_TENANTS)
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        == "engine"
    ]
    assert len(engines) == 3
    tenants = set()
    for deployment in engines:
        env = _env(deployment)
        assert env["ROBOTHOR_TENANT_ID"] == env["ROBOTHOR_DEFAULT_TENANT"]
        label = deployment["spec"]["template"]["metadata"]["labels"]["genus-os/tenant"]
        assert env["ROBOTHOR_TENANT_ID"] == label
        tenants.add(label)
        if label != "default":
            assert env["GENUS_EVENT_CONSUMER_GROUP"] == f"engine-{label}"
    assert tenants == {"default", "acme-child-a", "acme-child-b"}


def test_no_two_engine_services_select_the_same_pods() -> None:
    docs = _render(*TWO_TENANTS)
    services = [
        d
        for d in docs
        if d["kind"] == "Service" and d["spec"]["selector"].get("app.kubernetes.io/component") == "engine"
    ]
    pods = [
        d["spec"]["template"]["metadata"]["labels"]
        for d in docs
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        == "engine"
    ]
    assert len(services) == 3
    for service in services:
        selector = service["spec"]["selector"]
        matched = [p for p in pods if all(p.get(k) == v for k, v in selector.items())]
        assert len(matched) == 1, service["metadata"]["name"]


def test_engine_url_template_is_literal_and_resolves_to_a_real_service() -> None:
    docs = _render(*TWO_TENANTS)
    service_names = {d["metadata"]["name"] for d in docs if d["kind"] == "Service"}
    for name in ("gos-genus-os-bridge", "gos-genus-os-dashboard"):
        deployment = next(
            d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name
        )
        template = _env(deployment)["GENUS_TENANT_ENGINE_URL_TEMPLATE"]
        assert "{tenant}" in template
        host = template.format(tenant="acme-child-a").split("//", 1)[1].split(":", 1)[0]
        assert host in service_names


def test_lint_is_clean_with_tenants() -> None:
    result = subprocess.run(
        ["helm", "lint", str(CHART), "--strict", *TWO_TENANTS],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
