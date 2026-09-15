"""``scripts/build_plugin_index.py`` — what the registry's CI will run.

This is the other half of the signature. ``registry.py`` refuses everything
that does not verify; this builds the one document that does, and the two must
agree about the canonical bytes or the platform has a verifier nothing can
satisfy. So the test that matters most is the round trip: build an index here,
verify it with the shipped parser, and refuse it after one byte changes.

The rest is about not lying in the index. Every field a consumer acts on is
read out of the wheel itself -- the name and version from ``METADATA``, the
groups from ``entry_points.txt``, the ``manifest_sha256`` from the
``genus-plugin.yaml`` the loader would later read, and the verdict from the same
offline scanner the installer re-runs on download. A publisher who could type
those by hand would be marking their own homework in a signed document.

Wheels are assembled with ``zipfile`` rather than a build backend: this has to
run offline, in any checkout, and give byte-identical input every time.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from robothor.plugins import registry

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "build_plugin_index.py"

KEY_ID = "test-key-1"
#: The canonical manifest: it declares the contributed tool AND the entry
#: point that carries it, which is what lets the scan compare the wheel's
#: surface to the declaration by NAME rather than only by group.
_MANIFEST = (
    "name: acme-tools\n"
    "contract_version: 1\n"
    "handlers:\n"
    "  - probe\n"
    "entry_points:\n"
    "  genus.tools:\n"
    "    - acme\n"
)
_CODE = 'PLUGIN = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}\n'


def _builder():
    spec = importlib.util.spec_from_file_location("build_plugin_index", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def keys(tmp_path):
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "signing.pem"
    private.write_text(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    )
    private.chmod(0o600)
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, {KEY_ID: public_pem}


def _wheel(
    directory: Path,
    *,
    name: str = "acme-tools",
    module: str = "acme_tools",
    version: str = "1.2.3",
    code: str = _CODE,
    manifest: str | None = _MANIFEST,
    entry_points: str = "[genus.tools]\nacme = acme_tools:PLUGIN\n",
) -> Path:
    path = directory / f"{module}-{version}-py3-none-any.whl"
    dist_info = f"{module}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{module}/__init__.py", code)
        if manifest is not None:
            zf.writestr(f"{module}/genus-plugin.yaml", manifest)
        zf.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            "Summary: A probe tool\nRequires-Python: >=3.11\nLicense: MIT\n"
            "Project-URL: Homepage, https://example.invalid/acme\n",
        )
        zf.writestr(f"{dist_info}/entry_points.txt", entry_points)
        zf.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    return path


def _build(tmp_path, keys, *, base_url="https://example.invalid/wheels/", extra_args=()):
    private, _ = keys
    wheels = tmp_path / "wheels"
    out = tmp_path / "index.json"
    code = _builder().main(
        [
            str(wheels),
            "--out",
            str(out),
            "--key",
            str(private),
            "--key-id",
            KEY_ID,
            "--publisher",
            "genus",
            "--base-url",
            base_url,
            *extra_args,
        ]
    )
    return code, out


@pytest.fixture
def wheel_dir(tmp_path):
    directory = tmp_path / "wheels"
    directory.mkdir()
    return directory


def test_it_builds_an_index_the_shipped_parser_verifies(tmp_path, wheel_dir, keys) -> None:
    """The round trip. A builder and a verifier that disagree about the
    canonical bytes ship a registry nothing can publish to."""
    _wheel(wheel_dir)
    _wheel(
        wheel_dir,
        name="acme-jobs",
        module="acme_jobs",
        version="0.2.0",
        manifest="name: acme-jobs\ncontract_version: 1\njobs:\n  - nightly\n",
        entry_points="[genus.jobs]\nacme = acme_jobs:PLUGIN\n",
    )

    code, out = _build(tmp_path, keys)
    assert code == 0

    index = registry.parse_index(
        out.read_bytes(),
        Path(str(out) + ".sig").read_bytes(),
        keys=keys[1],
        url="https://example.invalid/index.json",
    )
    assert sorted(e.name for e in index.entries) == ["acme-jobs", "acme-tools"]
    assert index.publisher_id == "genus"
    assert index.key_id == KEY_ID


def test_one_byte_changed_afterwards_no_longer_verifies(tmp_path, wheel_dir, keys) -> None:
    _wheel(wheel_dir)
    _, out = _build(tmp_path, keys)
    tampered = out.read_bytes().replace(b'"1.2.3"', b'"1.2.4"', 1)
    with pytest.raises(registry.RegistryError):
        registry.parse_index(tampered, Path(str(out) + ".sig").read_bytes(), keys=keys[1])


def test_every_field_comes_out_of_the_wheel(tmp_path, wheel_dir, keys) -> None:
    path = _wheel(wheel_dir)
    _, out = _build(tmp_path, keys)
    entry = registry.parse_index(
        out.read_bytes(), Path(str(out) + ".sig").read_bytes(), keys=keys[1]
    ).entries[0]

    assert entry.name == "acme-tools"
    assert entry.version == "1.2.3"
    assert entry.summary == "A probe tool"
    assert entry.python == ">=3.11"
    assert entry.license == "MIT"
    assert entry.homepage == "https://example.invalid/acme"
    assert entry.groups == ("genus.tools",)
    assert entry.contract_version == "1"
    assert entry.manifest_sha256 == hashlib.sha256(_MANIFEST.encode()).hexdigest()

    artifact = entry.wheel()
    assert artifact is not None
    assert artifact.filename == path.name
    assert artifact.url == "https://example.invalid/wheels/" + path.name
    assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.size == path.stat().st_size


def test_the_verdict_is_the_scanner_not_the_publishers_opinion(tmp_path, wheel_dir, keys) -> None:
    _wheel(wheel_dir, code="import httpx\n" + _CODE)
    _, out = _build(tmp_path, keys)
    entry = registry.parse_index(
        out.read_bytes(), Path(str(out) + ".sig").read_bytes(), keys=keys[1]
    ).entries[0]
    assert entry.scan.verdict == "review"
    assert any("httpx" in reason for reason in entry.scan.reasons)
    assert entry.scan.scanned_at


def test_a_blocked_wheel_is_refused_at_publish_time(tmp_path, wheel_dir, keys, capsys) -> None:
    """A publisher who signs a blocked wheel into their index has signed an
    entry every installer will refuse. Say so at build time instead."""
    _wheel(wheel_dir, code="import ctypes\n" + _CODE)
    code, out = _build(tmp_path, keys)
    assert code == 2
    assert "ctypes" in capsys.readouterr().err
    assert not out.exists()


def test_publishing_a_blocked_wheel_is_possible_but_explicit(tmp_path, wheel_dir, keys) -> None:
    """--allow-blocked exists so a registry can carry a yanked-but-recorded
    entry; it must be a flag somebody typed, not the default."""
    _wheel(wheel_dir, code="import ctypes\n" + _CODE)
    code, out = _build(tmp_path, keys, extra_args=("--allow-blocked",))
    assert code == 0
    entry = registry.parse_index(
        out.read_bytes(), Path(str(out) + ".sig").read_bytes(), keys=keys[1]
    ).entries[0]
    assert entry.scan.verdict == "blocked"


def test_a_wheel_with_no_manifest_is_refused(tmp_path, wheel_dir, keys, capsys) -> None:
    _wheel(wheel_dir, manifest=None)
    code, _ = _build(tmp_path, keys)
    assert code == 2
    assert "genus-plugin.yaml" in capsys.readouterr().err


def test_an_empty_directory_is_refused_rather_than_signing_nothing(
    tmp_path, wheel_dir, keys, capsys
) -> None:
    """An index with no plugins is a valid document that silently removes every
    plugin the previous one published."""
    code, out = _build(tmp_path, keys)
    assert code == 2
    assert "no wheels" in capsys.readouterr().err
    assert not out.exists()


def test_the_written_file_is_canonical_and_freshly_dated(tmp_path, wheel_dir, keys) -> None:
    _wheel(wheel_dir)
    _, out = _build(tmp_path, keys)
    raw = out.read_bytes().rstrip(b"\n")
    assert registry.canonical_bytes(json.loads(raw)) == raw
    generated = datetime.fromisoformat(json.loads(raw)["generated_at"])
    assert datetime.now(UTC) - generated < timedelta(minutes=5)


def test_the_signature_file_names_the_key(tmp_path, wheel_dir, keys) -> None:
    _wheel(wheel_dir)
    _, out = _build(tmp_path, keys)
    doc = json.loads(Path(str(out) + ".sig").read_text())
    assert doc["key_id"] == KEY_ID
    assert doc["signature"]


def test_a_base_url_that_is_not_https_is_refused(tmp_path, wheel_dir, keys, capsys) -> None:
    _wheel(wheel_dir)
    code, _ = _build(tmp_path, keys, base_url="http://example.invalid/wheels/")
    assert code == 2
    assert "https" in capsys.readouterr().err


def test_a_yank_list_marks_entries_without_deleting_them(tmp_path, wheel_dir, keys) -> None:
    _wheel(wheel_dir)
    yanks = tmp_path / "yanked.json"
    yanks.write_text(json.dumps({"acme-tools==1.2.3": "key compromise"}))
    _, out = _build(tmp_path, keys, extra_args=("--yanked", str(yanks)))
    entry = registry.parse_index(
        out.read_bytes(), Path(str(out) + ".sig").read_bytes(), keys=keys[1]
    ).entries[0]
    assert entry.yanked is True
    assert entry.yank_reason == "key compromise"


def test_the_script_does_not_hold_a_private_key(tmp_path) -> None:
    """It reads one from a path its caller names. A signing key in the repo is
    a signing key in every clone."""
    source = SCRIPT.read_text()
    assert "PRIVATE KEY-----\nM" not in source
    assert "BEGIN PRIVATE KEY" not in source.replace("--key", "")
