"""``scripts/load-secrets.sh`` is the one thing that populates ``secrets.env``.

Before it existed, the systemd install path hard-required SOPS: the engine's
``ExecStartPre`` ran ``decrypt-secrets.sh``, and ``robothor-secrets.service``
carried ``ConditionPathExists=/etc/robothor/secrets.enc.json``. An operator
holding a plain ``secrets.env``, or one whose credentials already sit in the
unit environment, could not start the platform on systemd at all — the #1
systemd blocker in the productization gap analysis.

So the ExecStartPre becomes a dispatcher over three backends:

* ``sops`` — the existing ``decrypt-secrets.sh``, unchanged,
* ``file`` — a plaintext ``secrets.env`` the operator manages, validated and
  copied to tmpfs,
* ``env``  — nothing to load; write an EMPTY file so every consumer's
  ``EnvironmentFile=`` line still resolves.

Unset means auto-detect, so an existing SOPS box keeps working with no
configuration change at all.

Everything here runs under ``ROBOTHOR_SECRETS_ROOT``, a test seam that prefixes
``/etc/robothor`` and ``/run/robothor`` (the same ``--root`` idea as
``scripts/install-units.sh``), so every branch is exercised for real — as a
subprocess, with real files and real modes — without root and without touching
the box's own secrets.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
LOADER = SCRIPTS / "load-secrets.sh"
DECRYPT = SCRIPTS / "decrypt-secrets.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

#: A value that must never reach stdout, stderr, or a log line. Distinctive
#: enough that a substring search for it cannot match anything else.
SENTINEL = "sentinel-value-must-never-be-printed-7f3a1c"


# ── harness ──────────────────────────────────────────────────────────────────


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "etc" / "robothor").mkdir(parents=True)
    (root / "run").mkdir(parents=True)
    return root


def _run(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(LOADER)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ["PATH"],
            "ROBOTHOR_SECRETS_ROOT": str(root),
            **env,
        },
    )


def _output(root: Path) -> Path:
    return root / "run" / "robothor" / "secrets.env"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _write_plain_secrets(root: Path, *, mode: int = 0o600) -> Path:
    path = root / "etc" / "robothor" / "secrets.env"
    path.write_text(f'OPENROUTER_API_KEY="{SENTINEL}"\n')
    path.chmod(mode)
    return path


def _fake_sops(tmp_path: Path, payload: str) -> str:
    """A stub ``sops`` on PATH that prints *payload* for ``sops -d <file>``.

    ``decrypt-secrets.sh`` pins its own PATH to the system directories, so the
    stub is injected the only way a test is allowed to: ``ROBOTHOR_EXTRA_PATH``,
    the documented test seam every root script honours.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    sops = bin_dir / "sops"
    sops.write_text("#!/bin/bash\ncat <<'JSON'\n" + payload + "\nJSON\n")
    sops.chmod(0o755)
    return str(bin_dir)


def _sops_instance(tmp_path: Path, root: Path, payload: str) -> dict[str, str]:
    """A root that looks like a SOPS box, plus the env that makes it decryptable."""
    (root / "etc" / "robothor" / "secrets.enc.json").write_text("{}")
    (root / "etc" / "robothor" / "age.key").write_text("AGE-SECRET-KEY-STUB")
    return {"ROBOTHOR_EXTRA_PATH": _fake_sops(tmp_path, payload)}


FULL_PAYLOAD = '{"OPENROUTER_API_KEY": "' + SENTINEL + '", "OPENROUTER_API_KEY_2": "spare"}'


# ── the script exists and is shaped like its siblings ────────────────────────


def test_the_loader_exists_and_is_executable():
    assert LOADER.exists(), (
        "scripts/load-secrets.sh is the ExecStartPre every unit now runs — "
        "without it the units reference a script that is not there"
    )
    assert _mode(LOADER) & 0o111, "load-secrets.sh is not executable"


# ── backend: env ─────────────────────────────────────────────────────────────


def test_env_backend_writes_an_empty_file_so_environmentfile_resolves(tmp_path: Path):
    """The consumers load ``EnvironmentFile=-/run/robothor/secrets.env``.

    Optional or not, the file is the ordering contract between
    robothor-secrets.service and four services; an instance whose secrets come
    from the unit environment still needs it to exist, and to be empty rather
    than stale.
    """
    root = _root(tmp_path)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="env")
    assert result.returncode == 0, result.stdout + result.stderr
    out = _output(root)
    assert out.exists(), "the env backend wrote no file at all"
    assert out.read_text() == "", "the env backend must write an EMPTY file"
    assert _mode(out) == 0o600, f"secrets.env is mode {oct(_mode(out))}, not 0600"


def test_env_backend_truncates_a_file_left_by_another_backend(tmp_path: Path):
    """Switching to ``env`` must not leave yesterday's decrypted secrets on tmpfs."""
    root = _root(tmp_path)
    out = _output(root)
    out.parent.mkdir(parents=True)
    out.write_text(f'OPENROUTER_API_KEY="{SENTINEL}"\n')
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="env")
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.read_text() == "", "a stale secrets.env survived the env backend"


# ── backend: file ────────────────────────────────────────────────────────────


def test_file_backend_copies_a_wellformed_secrets_file(tmp_path: Path):
    root = _root(tmp_path)
    source = _write_plain_secrets(root)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="file")
    assert result.returncode == 0, result.stdout + result.stderr
    out = _output(root)
    assert out.read_text() == source.read_text()
    assert _mode(out) == 0o600, f"secrets.env is mode {oct(_mode(out))}, not 0600"


def test_file_backend_accepts_mode_0400(tmp_path: Path):
    """A read-only secrets file is a MORE careful operator, not a broken one."""
    root = _root(tmp_path)
    _write_plain_secrets(root, mode=0o400)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="file")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _mode(_output(root)) == 0o600


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o777])
def test_file_backend_refuses_a_readable_by_others_secrets_file(tmp_path: Path, mode: int):
    root = _root(tmp_path)
    _write_plain_secrets(root, mode=mode)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="file")
    assert result.returncode == 1, (
        f"mode {oct(mode)} exposes every credential the instance owns and was accepted"
    )
    assert "0600" in (result.stdout + result.stderr), (
        "the refusal must say what mode is expected, or the operator cannot fix it"
    )


def test_file_backend_refuses_a_missing_file(tmp_path: Path):
    root = _root(tmp_path)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="file")
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "secrets.env" in combined, "the refusal must name the path it looked for"


def test_file_backend_refuses_a_directory(tmp_path: Path):
    """A regular file, specifically: a directory at that path is a mistake, and
    ``-e`` alone would wave it through into a copy that fails obscurely."""
    root = _root(tmp_path)
    (root / "etc" / "robothor" / "secrets.env").mkdir()
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="file")
    assert result.returncode == 1
    assert "regular file" in (result.stdout + result.stderr).lower()


def test_file_backend_honours_an_explicit_path(tmp_path: Path):
    root = _root(tmp_path)
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text(f'OPENROUTER_API_KEY="{SENTINEL}"\n')
    elsewhere.chmod(0o600)
    result = _run(
        root,
        ROBOTHOR_SECRETS_BACKEND="file",
        ROBOTHOR_SECRETS_BACKEND_FILE=str(elsewhere),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _output(root).read_text() == elsewhere.read_text()


# ── backend: sops ────────────────────────────────────────────────────────────


def test_sops_backend_delegates_to_the_decrypt_script(tmp_path: Path):
    root = _root(tmp_path)
    env = _sops_instance(tmp_path, root, FULL_PAYLOAD)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="sops", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    out = _output(root)
    assert f'OPENROUTER_API_KEY="{SENTINEL}"' in out.read_text()
    assert _mode(out) == 0o600


def test_sops_backend_fails_when_the_decrypt_fails(tmp_path: Path):
    """``sops`` absent (or the age key wrong) must reach a human: nothing
    retries this oneshot, and four services sit in `dependency failed`."""
    root = _root(tmp_path)
    (root / "etc" / "robothor" / "secrets.enc.json").write_text("{}")
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="sops")
    assert result.returncode != 0, "a failed decrypt reported success"


# ── auto-detection ───────────────────────────────────────────────────────────


def test_auto_picks_sops_when_the_encrypted_file_is_there(tmp_path: Path):
    """The live box: every existing SOPS instance must keep working with no
    configuration change whatsoever."""
    root = _root(tmp_path)
    env = _sops_instance(tmp_path, root, FULL_PAYLOAD)
    result = _run(root, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sops" in result.stdout, "the chosen backend must be stated on one line"
    assert f'OPENROUTER_API_KEY="{SENTINEL}"' in _output(root).read_text()


def test_auto_picks_file_when_only_a_plaintext_file_is_there(tmp_path: Path):
    root = _root(tmp_path)
    _write_plain_secrets(root)
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "file" in result.stdout
    assert SENTINEL in _output(root).read_text()


def test_auto_falls_back_to_env_with_no_secrets_files_at_all(tmp_path: Path):
    """A fresh install has neither file. It must come UP, not fail — this is the
    whole reason the dispatcher exists."""
    root = _root(tmp_path)
    result = _run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "env" in result.stdout
    assert _output(root).read_text() == ""


def test_auto_prefers_sops_over_a_plaintext_file(tmp_path: Path):
    """Both present is ambiguous, and the encrypted one is the safer reading."""
    root = _root(tmp_path)
    _write_plain_secrets(root)
    env = _sops_instance(tmp_path, root, FULL_PAYLOAD)
    result = _run(root, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sops" in result.stdout


def test_an_unknown_backend_is_refused_by_name(tmp_path: Path):
    root = _root(tmp_path)
    result = _run(root, ROBOTHOR_SECRETS_BACKEND="vault-of-the-future")
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "vault-of-the-future" in combined
    for known in ("sops", "file", "env"):
        assert known in combined, "the refusal must list the backends that do exist"


# ── the thing that must never happen ─────────────────────────────────────────


@pytest.mark.parametrize("backend", ["sops", "file", "env", ""])
def test_no_backend_ever_prints_a_secret_value(tmp_path: Path, backend: str):
    """Every branch runs with a real credential present, and the value must not
    appear in stdout or stderr on any of them.

    ``systemd`` journals both streams: a script that echoed what it loaded
    would put the instance's whole credential set into a log every boot, where
    it outlives the tmpfs file it came from.
    """
    root = _root(tmp_path)
    _write_plain_secrets(root)
    env = _sops_instance(tmp_path, root, FULL_PAYLOAD)
    if backend:
        env["ROBOTHOR_SECRETS_BACKEND"] = backend
    result = _run(root, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert SENTINEL not in result.stdout, f"the {backend or 'auto'} backend printed a secret"
    assert SENTINEL not in result.stderr, f"the {backend or 'auto'} backend printed a secret"


def test_the_loader_repairs_its_own_path_first():
    """Same invariant as every other unit-started root script
    (tests/test_root_scripts_set_path.py), asserted here too because this one
    is started by five units and executes ``sops`` and ``install``."""
    from tests.test_root_scripts_set_path import missing_prelude

    assert not missing_prelude(LOADER), missing_prelude(LOADER)


# ── the units ────────────────────────────────────────────────────────────────


def test_no_unit_template_runs_the_sops_script_directly():
    """SOPS is one backend now. A unit that still runs ``decrypt-secrets.sh``
    is a unit that hard-requires it — the defect this change removes."""
    offenders = []
    for unit in sorted(UNIT_DIR.glob("*.service")):
        for raw in unit.read_text().splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if re.search(r"^(Exec\w+)=.*decrypt-secrets\.sh", line):
                offenders.append(f"{unit.name}: {line}")
    assert not offenders, (
        "these units execute the SOPS script directly instead of "
        "scripts/load-secrets.sh:\n  " + "\n  ".join(offenders)
    )


def test_the_secrets_unit_no_longer_skips_itself_without_sops():
    """``ConditionPathExists=/etc/robothor/secrets.enc.json`` made the ordering
    point a no-op on every instance that had no encrypted file — which is every
    instance the ``file`` and ``env`` backends exist for."""
    text = (UNIT_DIR / "robothor-secrets.service").read_text()
    live = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    conditions = [line for line in live if line.startswith("ConditionPathExists=")]
    assert not conditions, (
        "the secrets unit still conditions itself on a SOPS file: "
        f"{conditions} — an instance on the file or env backend would skip the "
        "one unit that populates secrets.env"
    )


def test_the_decrypt_script_is_still_shipped_as_the_sops_implementation():
    assert DECRYPT.exists(), (
        "load-secrets.sh delegates the sops backend to decrypt-secrets.sh; "
        "removing it would break every existing SOPS instance"
    )
