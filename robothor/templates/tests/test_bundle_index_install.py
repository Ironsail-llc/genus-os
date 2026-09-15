"""`genus agent install <slug>` against a SIGNED index.

The hub path installs what an unsigned API says; this one installs what a
publisher's key says, and it is the same signature, the same pinned keys and
the same canonical-bytes check the plugin installer uses. The two things worth
proving are that the artifact is pinned by the signed document (so swapping it
afterwards is caught) and that the two verbs cannot take each other's entries.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from robothor.plugins import registry
from robothor.templates.bundle_installer import BundleInstallError, install_bundle, plan_install
from robothor.templates.exporter import export_agent

KEY_ID = "test-key-1"
INDEX_URL = "https://example.invalid/index.json"

MANIFEST = """\
id: test-agent
name: Test Agent
description: A test agent
version: "1.0.0"
department: custom

model:
  primary: openrouter/xiaomi/mimo-v2-pro

schedule:
  cron: "0 * * * *"
  timezone: UTC

delivery:
  mode: none

tools_allowed: [read_file]
instruction_file: brain/agents/test-agent.md
"""

SCHEMA = {
    "required": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "string"},
        "department": {"type": "string", "enum": ["custom"]},
    }
}


def _workspace(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "brain" / "agents").mkdir(parents=True)
    (root / "templates" / "agents").mkdir(parents=True)
    (root / "docs" / "agents" / "schema.yaml").write_text(yaml.dump(SCHEMA))
    (root / "templates" / "agents" / "_defaults.yaml").write_text(yaml.dump({}))
    return root


@pytest.fixture
def archive(tmp_path):
    source = _workspace(tmp_path / "source")
    (source / "docs" / "agents" / "test-agent.yaml").write_text(MANIFEST)
    (source / "brain" / "agents" / "test-agent.md").write_text("# Test Agent\n")
    out = tmp_path / "test-agent.tar.gz"
    export_agent("test-agent", out=out, repo_root=source)
    return out


@pytest.fixture
def target(tmp_path):
    repo = _workspace(tmp_path / "target")
    instance_dir = tmp_path / "target-instance"
    instance_dir.mkdir()
    return repo, instance_dir


def _no_secrets(_name: str) -> str:
    return "missing"


def _kwargs(target, **extra):
    repo, instance_dir = target
    base = {
        "repo_root": repo,
        "instance_dir": instance_dir,
        "environment": {},
        "secret_lookup": _no_secrets,
        "adapter_dir": None,
        "index": INDEX_URL,
        "from_index": True,
    }
    base.update(extra)
    return base


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _publish(
    body: bytes,
    *,
    name: str = "test-agent",
    kind: str = "agent-bundle",
    serve: bytes | None = None,
    size: int | None = None,
):
    private_pem, public_pem = _keypair()
    entry: dict = {
        "kind": kind,
        "name": name,
        "version": "1.0.0",
        "artifacts": [
            {
                "kind": "bundle" if kind == "agent-bundle" else "wheel",
                "filename": "agent.tar.gz",
                "url": "https://example.invalid/agent.tar.gz",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body) if size is None else size,
            }
        ],
    }
    if kind != "agent-bundle":
        entry["manifest_sha256"] = "d" * 64
    payload = {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "publisher": {"id": "genus", "key_id": KEY_ID},
        "plugins": [entry],
    }
    raw = registry.canonical_bytes(payload) + b"\n"
    signature = registry.signature_document(payload, private_pem, key_id=KEY_ID).encode()
    served = body if serve is None else serve

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("index.json.sig"):
            return httpx.Response(200, content=signature)
        if path.endswith("index.json"):
            return httpx.Response(200, content=raw)
        if path.endswith("agent.tar.gz"):
            return httpx.Response(200, content=served)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), {KEY_ID: public_pem}


def test_a_published_bundle_installs(archive, target):
    repo, _ = target
    client, keys = _publish(archive.read_bytes())

    plan, result = install_bundle(
        "test-agent", yes=True, keys=keys, client=client, **_kwargs(target)
    )

    assert result is not None
    assert plan.sha256
    assert (repo / "docs" / "agents" / "test-agent.yaml").exists()


def test_a_plugin_entry_is_refused_by_the_agent_verb(archive, target):
    client, keys = _publish(archive.read_bytes(), name="genus-billing", kind="plugin")
    with pytest.raises(BundleInstallError, match="genus plugin install"):
        plan_install("genus-billing", keys=keys, client=client, **_kwargs(target))


def test_an_artifact_swapped_after_signing_is_refused(archive, target):
    """Refused for the RIGHT reason — a loose regex would pass on any refusal."""
    client, keys = _publish(archive.read_bytes(), serve=archive.read_bytes() + b"tampered")
    with pytest.raises(BundleInstallError) as excinfo:
        plan_install("test-agent", keys=keys, client=client, **_kwargs(target))
    assert "the signed index declares" in str(excinfo.value)


def test_an_artifact_the_index_mis_sizes_is_refused(archive, target):
    """The signed size is a fact an operator was shown; nobody was checking it."""
    body = archive.read_bytes()
    client, keys = _publish(body, size=10)
    with pytest.raises(BundleInstallError, match="bytes"):
        plan_install("test-agent", keys=keys, client=client, **_kwargs(target))


def test_a_correctly_sized_artifact_whose_bytes_changed_fails_the_hash(archive, tmp_path, target):
    """Same length, different bytes: the size passes and the SHA-256 catches it."""
    body = archive.read_bytes()
    swapped = body[:-1] + bytes([body[-1] ^ 0xFF])
    client, keys = _publish(body, serve=swapped)
    with pytest.raises(BundleInstallError, match="not the one you pinned"):
        plan_install("test-agent", keys=keys, client=client, **_kwargs(target))


def test_a_bundle_whose_id_is_not_the_published_name_is_refused(archive, target):
    """The signed name is the authority; the tarball must agree with it."""
    client, keys = _publish(archive.read_bytes(), name="other-agent")
    with pytest.raises(BundleInstallError, match="other-agent"):
        plan_install("other-agent", keys=keys, client=client, **_kwargs(target))


def test_an_unpinned_key_is_refused(archive, target):
    client, _ = _publish(archive.read_bytes())
    with pytest.raises(BundleInstallError, match="pinned|signature|key"):
        plan_install("test-agent", keys={}, client=client, **_kwargs(target))
