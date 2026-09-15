"""Install and remove, and everything that must refuse first.

Before this, the only way to add a plugin was ``pip install`` by hand: no
signature, no hash, no scan, no record of where the thing came from. The
pipeline here is the answer, and its ORDER is the design — every step refuses
before the next one can act, and pip is the LAST thing that happens rather than
the first:

    resolve -> verify the index signature -> download to a temp dir and check
    the sha256 -> open the wheel under the bounded rules -> check the manifest
    against the index's pin -> scan -> verdict -> pip -> record

Three rules are load-bearing and each has a test below that would fail loudly
if somebody "simplified" them:

* **pip is handed a list, never a string, and never a URL.** ``--no-deps``,
  ``--no-index`` and ``--find-links <our temp dir>`` mean pip resolves nothing
  and reaches nowhere. An install that lets a plugin pull arbitrary packages is
  the supply-chain hole; an install that hands pip a user-supplied URL or
  ``--index-url`` is the same hole with a different spelling.
* **Nothing is written on a refusal or a dry run.** Not the lockfile, not the
  environment. A ``--dry-run`` that left a row behind would be worse than no
  dry run at all.
* **The lock row records WHERE it came from.** Without it ``remove`` cannot
  tell a plugin this platform installed from one an operator pip-installed
  themselves, and would happily uninstall the latter.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - fixtures build real files
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from robothor.plugins import installer, lockfile, registry

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


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    return (
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


def _wheel_bytes(*, code: str = _CODE, manifest: str | None = _MANIFEST, extra=None) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("acme_tools/__init__.py", code)
        if manifest is not None:
            zf.writestr("acme_tools/genus-plugin.yaml", manifest)
        zf.writestr(
            "acme_tools-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: acme-tools\nVersion: 1.2.3\nSummary: A probe\n",
        )
        zf.writestr(
            "acme_tools-1.2.3.dist-info/entry_points.txt",
            "[genus.tools]\nacme = acme_tools:PLUGIN\n",
        )
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buffer.getvalue()


WHEEL_URL = "https://example.invalid/acme_tools-1.2.3-py3-none-any.whl"
INDEX_URL = "https://example.invalid/index.json"


def _index_payload(wheel: bytes, *, manifest_sha256: str | None = None, **overrides) -> dict:
    plugin = {
        "name": "acme-tools",
        "version": "1.2.3",
        "summary": "A probe",
        "contract_version": "1.0",
        "groups": ["genus.tools"],
        "python": ">=3.11",
        "artifacts": [
            {
                "kind": "wheel",
                "filename": "acme_tools-1.2.3-py3-none-any.whl",
                "url": WHEEL_URL,
                "sha256": hashlib.sha256(wheel).hexdigest(),
                "size": len(wheel),
            }
        ],
        "manifest_sha256": (
            manifest_sha256
            if manifest_sha256 is not None
            else hashlib.sha256(_MANIFEST.encode()).hexdigest()
        ),
        "scan": {"verdict": "safe", "reasons": [], "scanned_at": "2026-09-15T00:00:00+00:00"},
    }
    plugin.update(overrides)
    return {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "publisher": {"id": "genus", "key_id": KEY_ID},
        "plugins": [plugin],
    }


class _Pip:
    """A stand-in for the pip subprocess.

    Nothing in this file may install into the interpreter running the tests --
    the platform's own venv is not a scratch directory. Every command is
    recorded and nothing is executed.
    """

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command: list[str], **_: object):
        self.calls.append(list(command))
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


@pytest.fixture
def registry_fixture(tmp_path):
    """A signed index, a wheel, and a transport that serves both."""
    private_pem, public_pem = _keypair()
    wheel = _wheel_bytes()
    payload = _index_payload(wheel)
    raw = registry.canonical_bytes(payload)
    sig = registry.signature_document(payload, private_pem, key_id=KEY_ID).encode()

    served = {"index": raw, "sig": sig, "wheel": wheel}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".sig"):
            return httpx.Response(200, content=served["sig"])
        if path.endswith(".whl"):
            return httpx.Response(200, content=served["wheel"])
        return httpx.Response(200, content=served["index"])

    return SimpleNamespace(
        keys={KEY_ID: public_pem},
        private_pem=private_pem,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        served=served,
        lock=tmp_path / "plugins.lock",
        resign=lambda payload: (
            registry.canonical_bytes(payload),
            registry.signature_document(payload, private_pem, key_id=KEY_ID).encode(),
        ),
    )


def _install(fixture, **kwargs):
    kwargs.setdefault("pip", _Pip())
    return installer.install(
        "acme-tools",
        indexes=(INDEX_URL,),
        keys=fixture.keys,
        client=fixture.client,
        lock_path=fixture.lock,
        **kwargs,
    )


# --------------------------------------------------------------------------
# the lockfile's new source field
# --------------------------------------------------------------------------


def test_a_lockfile_written_before_source_existed_still_parses(tmp_path) -> None:
    """Additive, and pinned as additive. A platform upgrade that made every
    existing lockfile unreadable would re-enable every plugin an operator had
    turned off."""
    path = tmp_path / "plugins.lock"
    path.write_text(
        json.dumps(
            {
                "lockfile_version": 1,
                "plugins": [
                    {
                        "name": "acme-tools",
                        "version": "1.0.0",
                        "manifest_sha256": "",
                        "verdict": "unscanned",
                        "enabled": False,
                        "kinds": ["genus.tools"],
                        "recorded_at": "2026-09-01T00:00:00+00:00",
                    }
                ],
            }
        )
    )
    lock = lockfile.read_lockfile(path)
    assert not lock.malformed
    row = lock.row("acme-tools")
    assert row is not None
    assert row.enabled is False
    assert row.source is None


def test_a_row_with_no_source_does_not_serialise_one(tmp_path) -> None:
    row = lockfile.LockRow(name="acme-tools", version="1.0.0")
    assert "source" not in row.as_json()


def test_record_install_writes_the_source_and_sync_keeps_it(tmp_path) -> None:
    path = tmp_path / "plugins.lock"
    source = lockfile.LockSource(
        origin="registry",
        index_url=INDEX_URL,
        publisher_key_id=KEY_ID,
        installed_at="2026-09-15T00:00:00+00:00",
    )
    lockfile.record_install(
        "acme-tools",
        version="1.2.3",
        manifest_sha256="a" * 64,
        kinds=("genus.tools",),
        verdict="safe",
        dist_sha256="b" * 64,
        source=source,
        path=path,
    )
    row = lockfile.read_lockfile(path).row("acme-tools")
    assert row is not None
    assert row.verdict == "safe"
    assert row.dist_sha256 == "b" * 64
    assert row.source is not None
    assert row.source.index_url == INDEX_URL
    assert row.as_json()["source"]["publisher_key_id"] == KEY_ID

    # A later `genus plugin sync` rebuilds every row from what is installed.
    # It must not forget WHERE a plugin came from, or `remove` stops being able
    # to tell this platform's install from a hand-rolled one.
    from unittest.mock import patch

    dist = SimpleNamespace(name="acme-tools", version="1.2.3", files=(), read_text=lambda n: None)
    with (
        patch.object(lockfile, "installed_distributions", return_value={"acme-tools": dist}),
        patch.object(lockfile, "entry_point_groups", return_value={"acme-tools": {"genus.tools"}}),
    ):
        lockfile.sync(path)
    kept = lockfile.read_lockfile(path).row("acme-tools")
    assert kept is not None
    assert kept.source is not None
    assert kept.source.index_url == INDEX_URL
    assert kept.dist_sha256 == "b" * 64


def test_drop_row_removes_exactly_one(tmp_path) -> None:
    path = tmp_path / "plugins.lock"
    lockfile.write_lockfile(
        {
            "a": lockfile.LockRow(name="a"),
            "b": lockfile.LockRow(name="b"),
        },
        path,
    )
    assert lockfile.drop_row("a", path) is True
    assert lockfile.drop_row("a", path) is False
    assert sorted(lockfile.read_lockfile(path).rows) == ["b"]


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_installing_from_the_index_verifies_scans_and_records(registry_fixture) -> None:
    pip = _Pip()
    outcome = _install(registry_fixture, pip=pip)
    assert outcome.installed is True
    assert outcome.plan.verdict == "safe", outcome.plan.reasons
    assert outcome.plan.name == "acme-tools"
    assert outcome.plan.version == "1.2.3"
    assert outcome.plan.publisher_key_id == KEY_ID
    assert outcome.plan.sha256 == hashlib.sha256(registry_fixture.served["wheel"]).hexdigest()

    row = lockfile.read_lockfile(registry_fixture.lock).row("acme-tools")
    assert row is not None
    assert row.verdict == "safe"
    assert row.dist_sha256 == outcome.plan.sha256
    assert row.source is not None
    assert row.source.origin == "registry"
    assert row.source.index_url == INDEX_URL
    assert row.source.publisher_key_id == KEY_ID
    assert row.source.installed_at


def test_the_result_tells_the_operator_to_reload(registry_fixture) -> None:
    outcome = _install(registry_fixture)
    assert "SIGHUP" in outcome.reload_hint


# --------------------------------------------------------------------------
# pip is handed exactly what it is handed
# --------------------------------------------------------------------------


def test_pip_is_called_with_a_list_that_resolves_nothing(registry_fixture) -> None:
    pip = _Pip()
    _install(registry_fixture, pip=pip)
    assert len(pip.calls) == 1
    command = pip.calls[0]
    assert isinstance(command, list)
    assert command[1:4] == ["-m", "pip", "install"]
    assert "--no-deps" in command
    assert "--no-index" in command
    assert "--find-links" in command
    assert command[-1] == "acme-tools==1.2.3"


def test_pip_is_never_handed_a_url_or_an_index(registry_fixture) -> None:
    """The URL came out of the index. Handing it to pip would make pip the
    downloader, and pip does not check our signature or our hash."""
    pip = _Pip()
    _install(registry_fixture, pip=pip)
    command = pip.calls[0]
    assert not any(part.startswith(("http://", "https://")) for part in command)
    assert "--index-url" not in command
    assert "--extra-index-url" not in command
    assert "--pre" not in command


def test_the_plugin_dir_setting_installs_with_target(
    registry_fixture, tmp_path, monkeypatch
) -> None:
    from robothor.settings import reset_settings

    target = tmp_path / "plugins-target"
    monkeypatch.setenv("ROBOTHOR_PLUGIN_DIR", str(target))
    reset_settings()
    pip = _Pip()
    try:
        _install(registry_fixture, pip=pip)
    finally:
        reset_settings()
    command = pip.calls[0]
    assert "--target" in command
    assert str(target) in command


def test_a_failing_pip_is_a_refusal_and_writes_no_row(registry_fixture) -> None:
    pip = _Pip(returncode=1, stderr="could not install")
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, pip=pip)
    assert "could not install" in str(excinfo.value)
    assert lockfile.read_lockfile(registry_fixture.lock).row("acme-tools") is None


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_wheel_whose_hash_differs_from_the_index_is_refused(registry_fixture) -> None:
    registry_fixture.served["wheel"] = _wheel_bytes(code=_CODE + "# swapped\n")
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, pip=pip)
    assert "sha256" in str(excinfo.value).lower()
    assert pip.calls == []


def test_a_manifest_that_does_not_match_the_index_pin_is_refused(registry_fixture) -> None:
    """The index pins the manifest hash, so a wheel whose declaration was
    widened after publication is refused rather than installed and compared
    later — which is what the lockfile's drift check does, one import too
    late."""
    payload = _index_payload(registry_fixture.served["wheel"], manifest_sha256="c" * 64)
    raw, sig = registry_fixture.resign(payload)
    registry_fixture.served["index"], registry_fixture.served["sig"] = raw, sig
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, pip=pip)
    assert "manifest" in str(excinfo.value)
    assert pip.calls == []


def test_a_tampered_index_is_refused_before_anything_is_downloaded(registry_fixture) -> None:
    registry_fixture.served["index"] = registry_fixture.served["index"].replace(
        b'"1.2.3"', b'"1.2.4"'
    )
    pip = _Pip()
    with pytest.raises(registry.RegistryError):
        _install(registry_fixture, pip=pip)
    assert pip.calls == []


def test_a_blocked_wheel_is_refused_and_accept_review_does_not_help(registry_fixture) -> None:
    hostile = _wheel_bytes(code="import ctypes\n" + _CODE)
    registry_fixture.served["wheel"] = hostile
    payload = _index_payload(hostile)
    raw, sig = registry_fixture.resign(payload)
    registry_fixture.served["index"], registry_fixture.served["sig"] = raw, sig

    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, pip=pip, accept_review=True)
    assert "blocked" in str(excinfo.value)
    assert "ctypes" in str(excinfo.value)
    assert pip.calls == []


def test_a_review_wheel_needs_accept_review(registry_fixture) -> None:
    flagged = _wheel_bytes(code="import httpx\n" + _CODE)
    registry_fixture.served["wheel"] = flagged
    payload = _index_payload(flagged)
    raw, sig = registry_fixture.resign(payload)
    registry_fixture.served["index"], registry_fixture.served["sig"] = raw, sig

    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, pip=pip)
    assert "--accept-review" in str(excinfo.value)
    assert "httpx" in str(excinfo.value)
    assert pip.calls == []

    outcome = _install(registry_fixture, pip=_Pip(), accept_review=True)
    assert outcome.installed is True
    assert outcome.plan.verdict == "review"
    row = lockfile.read_lockfile(registry_fixture.lock).row("acme-tools")
    assert row is not None and row.verdict == "review"


def test_an_oversized_wheel_body_is_refused(registry_fixture, monkeypatch) -> None:
    monkeypatch.setattr(installer, "MAX_DOWNLOAD_BYTES", 16)
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, pip=pip)
    assert "too large" in str(excinfo.value)
    assert pip.calls == []


def test_a_version_the_index_does_not_publish_is_refused(registry_fixture) -> None:
    with pytest.raises(registry.RegistryError):
        _install(registry_fixture, version="9.9.9")


def test_the_wheel_cap_is_enforced_while_streaming_not_after_buffering(
    registry_fixture, monkeypatch
) -> None:
    """50 MB was reported, not enforced. The download ran inside the engine's
    admin thread, so a hostile mirror could OOM the daemon with one request the
    operator initiated."""
    produced = {"bytes": 0}
    chunk = b"x" * (64 * 1024)
    total = 300 * 1024 * 1024

    def body():
        while produced["bytes"] < total:
            produced["bytes"] += len(chunk)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".sig"):
            return httpx.Response(200, content=registry_fixture.served["sig"])
        if path.endswith(".whl"):
            return httpx.Response(200, content=body())
        return httpx.Response(200, content=registry_fixture.served["index"])

    registry_fixture.client = httpx.Client(transport=httpx.MockTransport(handler))
    # The signed `size` pre-check would refuse first; this is about the byte
    # counter, so lift that gate and leave only the counter standing.
    monkeypatch.setattr(installer, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(
            "acme-tools",
            indexes=(INDEX_URL,),
            keys=registry_fixture.keys,
            client=registry_fixture.client,
            lock_path=registry_fixture.lock,
            pip=pip,
        )
    assert "too large" in str(excinfo.value)
    assert produced["bytes"] <= installer.MAX_DOWNLOAD_BYTES * 2, (
        f"read {produced['bytes']} bytes for a {installer.MAX_DOWNLOAD_BYTES}-byte cap"
    )
    assert pip.calls == []


def test_a_declared_content_length_over_the_wheel_cap_is_refused_first(
    registry_fixture, monkeypatch
) -> None:
    sent = {"bytes": 0}

    def body():
        sent["bytes"] += 1
        yield b"x" * 4096

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".sig"):
            return httpx.Response(200, content=registry_fixture.served["sig"])
        if path.endswith(".whl"):
            return httpx.Response(200, headers={"content-length": str(10**12)}, content=body())
        return httpx.Response(200, content=registry_fixture.served["index"])

    monkeypatch.setattr(installer, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(
            "acme-tools",
            indexes=(INDEX_URL,),
            keys=registry_fixture.keys,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            lock_path=registry_fixture.lock,
            pip=_Pip(),
        )
    assert "too large" in str(excinfo.value)
    assert sent["bytes"] == 0


# --------------------------------------------------------------------------
# I2 — the pip argv is built from validated metadata
# --------------------------------------------------------------------------


def _hostile_metadata_wheel(tmp_path, *, name="acme-tools", version="1.2.3") -> tuple[Path, str]:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("acme_tools/__init__.py", _CODE)
        zf.writestr("acme_tools/genus-plugin.yaml", _MANIFEST)
        zf.writestr(
            "acme_tools-1.2.3.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: A probe\n",
        )
        zf.writestr(
            "acme_tools-1.2.3.dist-info/entry_points.txt",
            "[genus.tools]\nacme = acme_tools:PLUGIN\n",
        )
    data = buffer.getvalue()
    path = tmp_path / "acme_tools-1.2.3-py3-none-any.whl"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("--find-links=https://evil.example.org/", "1.0"),
        ("--index-url=https://evil.example.org/simple", "1.0"),
        ("acme-tools", "1.0 --pre"),
        ("acme tools", "1.0"),
        ("-r/etc/passwd", "1.0"),
        ("acme-tools", "--extra-index-url=https://evil.example.org/"),
        ("acme-tools", "not a version"),
    ],
)
def test_hostile_wheel_metadata_never_reaches_pip(tmp_path, name, version) -> None:
    """The requirement token was built straight out of the downloaded wheel's
    METADATA. The module's own docstring says pip "never sees a URL, never an
    --index-url, and never --pre"; three of these made that false, and the test
    that was supposed to catch it asserted exact token membership, which
    '--index-url=https://…==1.0' is not."""
    path, digest = _hostile_metadata_wheel(tmp_path, name=name, version=version)
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(str(path), sha256=digest, lock_path=tmp_path / "plugins.lock", pip=pip)
    message = str(excinfo.value)
    assert "METADATA" in message
    assert pip.calls == []


def test_no_pip_token_after_our_own_flags_can_look_like_an_option(tmp_path) -> None:
    """The invariant stated positively: whatever the wheel called itself, the
    only token after --find-links <dir> and our own switches is a requirement."""
    path, digest = _hostile_metadata_wheel(tmp_path)
    pip = _Pip()
    installer.install(str(path), sha256=digest, lock_path=tmp_path / "plugins.lock", pip=pip)
    command = pip.calls[0]
    ours = {
        "--no-deps",
        "--no-index",
        "--find-links",
        "--disable-pip-version-check",
        "--no-input",
        "--target",
        "--upgrade",
    }
    tail = command[command.index("--find-links") + 2 :]
    assert [t for t in tail if t.startswith("-") and t not in ours] == []
    assert tail[-1] == "acme-tools==1.2.3"


def test_a_registry_installed_plugin_is_name_enforced_whatever_the_mode(
    tmp_path, monkeypatch
) -> None:
    """The group-granularity trade was defended by pointing at the loader's
    post-import name check -- which only enforces under
    ROBOTHOR_PLUGIN_MANIFEST_MODE=enforce, and the shipped default is
    `observe`. A plugin THIS PLATFORM installed has no claim on that
    grandfathering: it could not have been installed without a manifest the
    index pinned."""
    from unittest.mock import patch

    from robothor.plugins import loader

    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "observe")
    lock = tmp_path / "plugins.lock"
    lockfile.record_install(
        "acme-tools",
        version="1.2.3",
        # The REAL digest: a mismatch would be refused as drift before the
        # name check this test is about could run.
        manifest_sha256=hashlib.sha256(_MANIFEST.encode()).hexdigest(),
        kinds=("genus.tools",),
        verdict="safe",
        dist_sha256="b" * 64,
        source=lockfile.LockSource(origin="registry", installed_at="2026-09-15T00:00:00+00:00"),
        path=lock,
    )

    class _Dist:
        name, version, files = "acme-tools", "1.2.3", ()

        def read_text(self, filename):
            return _MANIFEST if filename == "genus-plugin.yaml" else None

    class _EP:
        name, group = "acme", "genus.tools"
        dist = _Dist()

        def load(self):
            # Contributes a tool the manifest never declared.
            return {
                "genus_contract_version": "1.0",
                "handlers": {"probe": lambda: None, "undeclared": lambda: None},
            }

    with patch.object(loader, "_discover", lambda: [_EP()]):
        result = loader.load_plugins(lockfile_path=lock)
    assert not result.loaded
    assert any("undeclared" in f.reason for f in result.failures), result.failures


def test_a_hand_installed_plugin_keeps_the_observe_grandfathering(tmp_path, monkeypatch) -> None:
    """The other half: a distribution with no lock source is one somebody
    pip-installed, and `observe` exists so that upgrading Genus does not refuse
    every plugin published before manifests did."""
    from unittest.mock import patch

    from robothor.plugins import loader

    monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "observe")
    lock = tmp_path / "plugins.lock"
    lockfile.write_lockfile({}, lock)

    class _Dist:
        name, version, files = "acme-tools", "1.2.3", ()

        def read_text(self, filename):
            return _MANIFEST if filename == "genus-plugin.yaml" else None

    class _EP:
        name, group = "acme", "genus.tools"
        dist = _Dist()

        def load(self):
            return {
                "genus_contract_version": "1.0",
                "handlers": {"probe": lambda: None, "undeclared": lambda: None},
            }

    with patch.object(loader, "_discover", lambda: [_EP()]):
        result = loader.load_plugins(lockfile_path=lock, reserved_names=set())
    assert result.loaded


def test_a_valid_pep440_local_version_is_accepted(tmp_path) -> None:
    path, digest = _hostile_metadata_wheel(tmp_path, version="1.0+acme1")
    pip = _Pip()
    installer.install(str(path), sha256=digest, lock_path=tmp_path / "plugins.lock", pip=pip)
    assert pip.calls[0][-1] == "acme-tools==1.0+acme1"


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_runs_no_pip(registry_fixture) -> None:
    pip = _Pip()
    outcome = _install(registry_fixture, pip=pip, dry_run=True)
    assert outcome.installed is False
    assert outcome.dry_run is True
    assert outcome.plan.verdict == "safe"
    assert pip.calls == []
    assert not registry_fixture.lock.exists()


def test_a_dry_run_shows_the_pip_command_it_would_run(registry_fixture) -> None:
    outcome = _install(registry_fixture, dry_run=True)
    assert outcome.plan.pip_command
    assert outcome.plan.pip_command[-1] == "acme-tools==1.2.3"


def test_a_dry_run_of_a_blocked_wheel_still_refuses(registry_fixture) -> None:
    hostile = _wheel_bytes(code="import os\nos.system('id')\n" + _CODE)
    registry_fixture.served["wheel"] = hostile
    payload = _index_payload(hostile)
    raw, sig = registry_fixture.resign(payload)
    registry_fixture.served["index"], registry_fixture.served["sig"] = raw, sig
    with pytest.raises(installer.InstallError) as excinfo:
        _install(registry_fixture, dry_run=True)
    assert "os.system" in str(excinfo.value)


# --------------------------------------------------------------------------
# offline wheel install
# --------------------------------------------------------------------------


def test_a_local_wheel_needs_an_explicit_sha256(tmp_path) -> None:
    path = tmp_path / "acme_tools-1.2.3-py3-none-any.whl"
    path.write_bytes(_wheel_bytes())
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(str(path), lock_path=tmp_path / "plugins.lock", pip=_Pip())
    assert "--sha256" in str(excinfo.value)


def test_a_local_wheel_with_the_wrong_sha256_is_refused(tmp_path) -> None:
    path = tmp_path / "acme_tools-1.2.3-py3-none-any.whl"
    path.write_bytes(_wheel_bytes())
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(str(path), sha256="d" * 64, lock_path=tmp_path / "plugins.lock", pip=pip)
    assert "sha256" in str(excinfo.value).lower()
    assert pip.calls == []


def test_a_local_wheel_installs_and_records_a_wheel_source(tmp_path) -> None:
    data = _wheel_bytes()
    path = tmp_path / "acme_tools-1.2.3-py3-none-any.whl"
    path.write_bytes(data)
    lock = tmp_path / "plugins.lock"
    pip = _Pip()
    outcome = installer.install(
        str(path), sha256=hashlib.sha256(data).hexdigest(), lock_path=lock, pip=pip
    )
    assert outcome.installed is True
    row = lockfile.read_lockfile(lock).row("acme-tools")
    assert row is not None
    assert row.source is not None
    assert row.source.origin == "wheel"
    assert row.source.index_url == ""
    assert row.source.publisher_key_id == ""
    assert pip.calls[0][-1] == "acme-tools==1.2.3"


def test_a_wheel_path_that_does_not_exist_is_a_sentence(tmp_path) -> None:
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(
            str(tmp_path / "nope.whl"),
            sha256="e" * 64,
            lock_path=tmp_path / "plugins.lock",
            pip=_Pip(),
        )
    assert "nope.whl" in str(excinfo.value)


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------


def test_remove_uninstalls_and_drops_the_row(registry_fixture) -> None:
    _install(registry_fixture)
    pip = _Pip()
    result = installer.remove("acme-tools", lock_path=registry_fixture.lock, pip=pip)
    assert result["removed"] is True
    assert pip.calls[0][1:] == ["-m", "pip", "uninstall", "-y", "acme-tools"]
    assert lockfile.read_lockfile(registry_fixture.lock).row("acme-tools") is None
    assert "SIGHUP" in result["reload_hint"]


def test_remove_refuses_a_distribution_this_platform_did_not_install(tmp_path) -> None:
    """A row with no ``source`` is one ``genus plugin sync`` recorded from a
    distribution somebody pip-installed by hand. Uninstalling it because it
    appears in our lockfile would be this platform reaching outside what it
    owns."""
    lock = tmp_path / "plugins.lock"
    lockfile.write_lockfile({"acme-tools": lockfile.LockRow(name="acme-tools")}, lock)
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        installer.remove("acme-tools", lock_path=lock, pip=pip)
    assert "--force" in str(excinfo.value)
    assert pip.calls == []


def test_remove_force_goes_ahead(tmp_path) -> None:
    lock = tmp_path / "plugins.lock"
    lockfile.write_lockfile({"acme-tools": lockfile.LockRow(name="acme-tools")}, lock)
    pip = _Pip()
    result = installer.remove("acme-tools", force=True, lock_path=lock, pip=pip)
    assert result["removed"] is True
    assert pip.calls


@pytest.mark.parametrize(
    "name",
    ["--requirement=/etc/passwd", "-r/etc/passwd", "acme tools", "../../etc/passwd", ""],
)
def test_remove_validates_the_name_before_pip_sees_it(tmp_path, name) -> None:
    """Both HTTP surfaces validated; the CLI did not, and both callers share
    this function. ``genus plugin remove -- --requirement=/path/reqs.txt``
    reached pip as an option."""
    lock = tmp_path / "plugins.lock"
    lockfile.write_lockfile({}, lock)
    pip = _Pip()
    with pytest.raises(installer.InstallError) as excinfo:
        installer.remove(name, force=True, lock_path=lock, pip=pip)
    assert "distribution name" in str(excinfo.value)
    assert pip.calls == []


def test_remove_of_an_unknown_name_is_a_refusal(tmp_path) -> None:
    lock = tmp_path / "plugins.lock"
    lockfile.write_lockfile({}, lock)
    with pytest.raises(installer.InstallError) as excinfo:
        installer.remove("nothing-here", lock_path=lock, pip=_Pip())
    assert "nothing-here" in str(excinfo.value)


def test_remove_drops_the_row_even_when_pip_says_it_was_not_installed(
    registry_fixture,
) -> None:
    """pip exits non-zero for "not installed". Leaving the row behind would
    make the lockfile permanently describe something that is gone."""
    _install(registry_fixture)
    pip = _Pip(returncode=1, stderr="WARNING: Skipping acme-tools as it is not installed.")
    result = installer.remove("acme-tools", lock_path=registry_fixture.lock, pip=pip)
    assert result["removed"] is True
    assert lockfile.read_lockfile(registry_fixture.lock).row("acme-tools") is None
    assert "not installed" in result["note"]


# --------------------------------------------------------------------------
# what crosses an API boundary
# --------------------------------------------------------------------------


def test_the_plan_json_carries_no_path_by_default(registry_fixture, tmp_path) -> None:
    outcome = _install(registry_fixture, dry_run=True)
    payload = json.dumps(outcome.as_json())
    assert str(tmp_path) not in payload
    assert "pip_command" not in payload
    assert "/tmp" not in payload


def test_the_plan_json_can_include_the_command_for_the_cli(registry_fixture) -> None:
    outcome = _install(registry_fixture, dry_run=True)
    payload = outcome.as_json(include_command=True)
    assert payload["plan"]["pip_command"][-1] == "acme-tools==1.2.3"


def test_the_plan_json_shape_is_what_the_ui_reads(registry_fixture) -> None:
    outcome = _install(registry_fixture, dry_run=True)
    payload = outcome.as_json()
    assert set(payload) == {"plan", "installed", "dry_run", "row", "reload_hint", "note"}
    assert set(payload["plan"]) == {
        "name",
        "version",
        "origin",
        "index_url",
        "publisher_key_id",
        "filename",
        "sha256",
        "size",
        "verdict",
        "reasons",
        "prompt_scan",
        "groups",
        "accept_review",
        "summary",
    }
