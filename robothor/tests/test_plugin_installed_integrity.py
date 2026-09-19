"""An accepted wheel hash is not evidence that its installed files still match."""

import hashlib
import json
import zipfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    payloads = {
        "adapter/__init__.py": b"raise RuntimeError('do not import to verify')\n",
        "adapter/genus-plugin.yaml": b"name: adapter\ncontract_version: 1\nentry_points:\n  genus.services: [adapter]\nservices: [sales.business.example]\n",
        "adapter-1.0.dist-info/METADATA": b"Name: adapter\nVersion: 1.0\n",
        "adapter-1.0.dist-info/entry_points.txt": b"[genus.services]\nadapter = adapter:SERVICES\n",
        "adapter-1.0.dist-info/RECORD": b"",
    }
    wheel_path = tmp_path / "adapter-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for path, data in payloads.items():
            wheel.writestr(path, data)
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    wheel_sha = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    lock = tmp_path / "plugins.lock"
    lock.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": [
                    {
                        "name": "adapter",
                        "version": "1.0",
                        "manifest_sha256": hashlib.sha256(
                            payloads["adapter/genus-plugin.yaml"]
                        ).hexdigest(),
                        "dist_sha256": wheel_sha,
                        "enabled": True,
                        "verdict": "safe",
                        "source": {"origin": "wheel", "installed_at": "2026-09-19T00:00:00Z"},
                    }
                ],
            }
        )
    )
    distribution = SimpleNamespace(
        version="1.0",
        metadata={"Name": "adapter"},
        files=list(payloads),
        locate_file=lambda path: root / path,
    )
    return wheel_path, wheel_sha, lock, distribution, root


def test_verifies_exact_installed_payload_without_importing_plugin(installed):
    from robothor.plugins.installed import verify_installed_wheel

    wheel, digest, lock, distribution, root = installed
    (root / "adapter-1.0.dist-info/INSTALLER").write_text("uv\n")
    receipt = verify_installed_wheel(
        wheel, expected_digest=digest, lock_path=lock, distribution=distribution
    )
    assert receipt["name"] == "adapter"
    assert receipt["wheel_sha256"] == digest
    assert receipt["payload_files_verified"] == 4


@pytest.mark.parametrize(
    "fault",
    ["code", "missing", "extra", "symlink", "version", "lock", "disabled", "wheel", "unlisted"],
)
def test_installed_or_accepted_artifact_drift_is_refused(installed, fault):
    from robothor.plugins.installed import verify_installed_wheel

    wheel, digest, lock, distribution, root = installed
    target = root / "adapter/__init__.py"
    if fault == "code":
        target.write_text("Changed code")
    elif fault == "missing":
        target.unlink()
    elif fault == "extra":
        (root / "adapter/unlisted.py").write_text("Extra code")
    elif fault == "symlink":
        target.unlink()
        target.symlink_to(wheel)
    elif fault == "version":
        distribution.version = "2.0"
    elif fault == "lock":
        lock.write_text("invalid")
    elif fault == "disabled":
        data = json.loads(lock.read_text())
        data["plugins"][0]["enabled"] = False
        lock.write_text(json.dumps(data))
    elif fault == "wheel":
        wheel.write_bytes(b"Changed wheel")
    else:
        distribution.files.remove("adapter/__init__.py")
    with pytest.raises(ValueError):
        verify_installed_wheel(
            wheel, expected_digest=digest, lock_path=lock, distribution=distribution
        )


def test_wheel_swap_after_digest_check_cannot_change_what_is_verified(installed, monkeypatch):
    import io

    from robothor.plugins import installed as verifier

    wheel, digest, lock, distribution, root = installed
    original_open = verifier.open_wheel

    def swap_then_open(*args, **kwargs):
        with zipfile.ZipFile(wheel) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["adapter/__init__.py"] = b"Changed after hash check"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        wheel.write_bytes(buffer.getvalue())
        (root / "adapter/__init__.py").write_bytes(members["adapter/__init__.py"])
        return original_open(*args, **kwargs)

    monkeypatch.setattr(verifier, "open_wheel", swap_then_open)
    with pytest.raises(ValueError):
        verifier.verify_installed_wheel(
            wheel, expected_digest=digest, lock_path=lock, distribution=distribution
        )
