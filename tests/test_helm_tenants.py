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

#: Tenant cells need the one vault key every engine and the bridge share.
SHARED_VAULT_KEY = [
    "--set",
    "vaultKey.enabled=true",
    "--set",
    "vault.secrets.vault-key.path=genus-os/test/vault-key",
]

TWO_TENANTS = [
    *SHARED_VAULT_KEY,
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
    before = _kind_names(_render(*SHARED_VAULT_KEY, "--set", "workspace.persistence.enabled=true"))
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
        if d["kind"] == "Service"
        and d["spec"]["selector"].get("app.kubernetes.io/component") == "engine"
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


def test_every_vault_reader_shares_one_key() -> None:
    """The bridge stores a child's credential; the child's engine decrypts it."""
    readers = {}
    for doc in _render(*TWO_TENANTS):
        if doc["kind"] != "Deployment":
            continue
        labels = doc["spec"]["template"]["metadata"]["labels"]
        if labels.get("app.kubernetes.io/component") not in ("engine", "bridge"):
            continue
        volumes = doc["spec"]["template"]["spec"]["volumes"]
        secret = next(v["secret"]["secretName"] for v in volumes if v["name"] == "vault-key")
        env = _env(doc)
        readers[doc["metadata"]["name"]] = (
            secret,
            env["GENUS_VAULT_KEY_FILE"],
            env["GENUS_FINGERPRINT_SALT_FILE"],
        )
    assert set(readers) == {
        "gos-genus-os-bridge",
        "gos-genus-os-engine",
        "gos-genus-os-engine-acme-child-a",
        "gos-genus-os-engine-acme-child-b",
    }
    assert len(set(readers.values())) == 1, readers
    # The agent secret-file denylist guards these basenames.
    _, key_file, salt_file = next(iter(readers.values()))
    assert key_file.endswith("/.vault-key")
    assert salt_file.endswith("/.fingerprint-salt")


def test_tenants_without_the_shared_key_are_refused() -> None:
    result = subprocess.run(
        ["helm", "template", "gos", str(CHART), "--set", "tenants[0].id=acme-child-a"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "vaultKey.enabled" in result.stderr


def test_no_engine_can_write_its_own_import_path() -> None:
    """Code on an engine's import path runs inside the engine.

    The engine's agents write its workspace (``write_file``, ``exec``), so a
    plugin directory or ``PYTHONPATH`` entry on that workspace has to be
    mounted again read-only, for the root engine and for every cell, and
    created by an init container running as the engine's own user first.
    """
    docs = _render(
        *TWO_TENANTS,
        "--set",
        "engine.env.ROBOTHOR_PLUGIN_DIR=/workspace/.plugins",
        "--set",
        "engine.env.PYTHONPATH=/workspace/.plugins",
    )
    engines = [
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        == "engine"
    ]
    assert len(engines) == 3
    for doc in engines:
        name = doc["metadata"]["name"]
        pod = doc["spec"]["template"]["spec"]
        engine = pod["containers"][0]
        env = _env(doc)
        workspace = env["ROBOTHOR_WORKSPACE"].rstrip("/")
        import_dirs = {env["ROBOTHOR_PLUGIN_DIR"], *env["PYTHONPATH"].split(":")}
        on_workspace = {d.rstrip("/") for d in import_dirs if d.startswith(workspace + "/")}
        assert on_workspace, name
        mounts = {m["mountPath"]: m for m in engine["volumeMounts"]}
        for path in on_workspace:
            mount = mounts.get(path)
            assert mount is not None, (name, path)
            assert mount["name"] == "workspace", (name, path)
            assert mount["readOnly"] is True, (name, path)
            assert mount["subPath"] == path[len(workspace) + 1 :], (name, path)
        init = {c["name"]: c for c in pod.get("initContainers", [])}
        assert "plugin-dirs" in init, name
        assert init["plugin-dirs"]["image"] == engine["image"], name
        assert init["plugin-dirs"]["securityContext"] == engine["securityContext"], name
        assert init["plugin-dirs"]["command"][:2] == ["sh", "-c"], name
        assert set(init["plugin-dirs"]["command"][4:]) == on_workspace, name


def _plugin_dirs_command(workspace_mount: str) -> list[str]:
    docs = _render(*TWO_TENANTS, "--set", f"workspace.mountPath={workspace_mount}")
    cell = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("acme-child-a")
    )
    init = next(
        c for c in cell["spec"]["template"]["spec"]["initContainers"] if c["name"] == "plugin-dirs"
    )
    return init["command"]


def test_the_plugin_dir_init_step_creates_a_missing_directory(tmp_path: Path) -> None:
    command = _plugin_dirs_command(str(tmp_path))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".plugins").is_dir()
    assert not (tmp_path / ".plugins").is_symlink()


@pytest.mark.parametrize("target", [".", "other"])
def test_the_plugin_dir_init_step_refuses_a_symlinked_directory(
    tmp_path: Path, target: str
) -> None:
    """A pre-upgrade agent could have replaced ``.plugins`` with a link to the
    writable workspace; a read-only mount of that link would be a read-only
    view of a directory still writable through the workspace mount."""
    (tmp_path / "other").mkdir()
    (tmp_path / ".plugins").symlink_to(target)
    command = _plugin_dirs_command(str(tmp_path))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "symlink" in result.stderr


def test_the_plugin_dir_init_step_refuses_a_symlinked_workspace_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "ws").symlink_to(real)
    command = _plugin_dirs_command(str(tmp_path / "ws"))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0


def test_the_plugin_dir_init_step_refuses_a_symlink_inside_the_directory(tmp_path: Path) -> None:
    """A real ``.plugins`` holding ``somepkg -> ../scratch/somepkg`` would let
    Python import code that is still writable through the workspace mount;
    wheels never ship symlinks, so a legitimate install never trips this."""
    (tmp_path / "scratch" / "somepkg").mkdir(parents=True)
    (tmp_path / ".plugins").mkdir()
    (tmp_path / ".plugins" / "somepkg").symlink_to("../scratch/somepkg")
    command = _plugin_dirs_command(str(tmp_path))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "contains a symlink" in result.stderr


def test_the_plugin_dir_init_step_accepts_an_ordinary_installed_tree(tmp_path: Path) -> None:
    pkg = tmp_path / ".plugins" / "acme_plugin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (tmp_path / ".plugins" / "acme_plugin-1.0.dist-info").mkdir()
    command = _plugin_dirs_command(str(tmp_path))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
