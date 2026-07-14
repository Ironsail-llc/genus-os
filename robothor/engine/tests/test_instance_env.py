"""A CLI run must obey the same guardrails as the daemon.

The daemon gets its guardrail/feature-flag posture from systemd:
``EnvironmentFile=/etc/robothor/robothor.env`` plus the ``Environment=`` lines
in the ``robothor-engine.service.d`` drop-in. A CLI invocation
(``robothor engine run ...``) inherits only the invoking shell's environment —
so it executed with every rollout-gated guardrail OFF while the daemon
enforced them. An operator (or an agent shelling out) silently escaped
enforcement.

``load_instance_env`` closes that: it reads the same two sources the unit
does, and never overrides a variable the caller set explicitly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from robothor.engine.instance_env import load_instance_env

if TYPE_CHECKING:
    from pathlib import Path


def _write_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "robothor.env"
    env_file.write_text(
        "# comment\n"
        "ROBOTHOR_TENANT_ID=acme\n"
        'ROBOTHOR_QUOTED="quoted-value"\n'
        "\n"
        "ROBOTHOR_INJECTION_SCAN_ENABLED=1\n"
    )
    return env_file


def _write_dropin(tmp_path: Path) -> Path:
    d = tmp_path / "robothor-engine.service.d"
    d.mkdir()
    (d / "upgrade-rip-flags.conf").write_text(
        "# Guardrail ladder\n"
        "[Service]\n"
        "Environment=ROBOTHOR_INJECTION_SCAN_MODE=enforce\n"
        'Environment="ROBOTHOR_RBAC_MODE=enforce"\n'
    )
    # a .bak file must be ignored — only *.conf is live
    (d / "upgrade-rip-flags.conf.bak").write_text("Environment=ROBOTHOR_RBAC_MODE=observe\n")
    return d


def test_loads_guardrail_modes_from_dropin(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_INJECTION_SCAN_MODE", raising=False)
    monkeypatch.delenv("ROBOTHOR_RBAC_MODE", raising=False)

    load_instance_env(env_file=_write_env(tmp_path), dropin_dir=_write_dropin(tmp_path))

    assert os.environ["ROBOTHOR_INJECTION_SCAN_MODE"] == "enforce"
    assert os.environ["ROBOTHOR_RBAC_MODE"] == "enforce", "quoted Environment= not parsed"


def test_loads_env_file_values(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
    monkeypatch.delenv("ROBOTHOR_QUOTED", raising=False)

    load_instance_env(env_file=_write_env(tmp_path), dropin_dir=_write_dropin(tmp_path))

    assert os.environ["ROBOTHOR_TENANT_ID"] == "acme"
    assert os.environ["ROBOTHOR_QUOTED"] == "quoted-value", "quotes should be stripped"


def test_never_overrides_an_explicitly_set_variable(tmp_path, monkeypatch):
    """A caller's explicit env wins — this must not clobber a deliberate override."""
    monkeypatch.setenv("ROBOTHOR_INJECTION_SCAN_MODE", "observe")

    load_instance_env(env_file=_write_env(tmp_path), dropin_dir=_write_dropin(tmp_path))

    assert os.environ["ROBOTHOR_INJECTION_SCAN_MODE"] == "observe"


def test_missing_files_are_not_an_error(tmp_path):
    """A dev box without /etc/robothor must still run."""
    load_instance_env(env_file=tmp_path / "nope.env", dropin_dir=tmp_path / "nope.d")


def test_backup_dropin_files_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_RBAC_MODE", raising=False)

    load_instance_env(env_file=_write_env(tmp_path), dropin_dir=_write_dropin(tmp_path))

    # the .bak says observe; only the live .conf must be read
    assert os.environ["ROBOTHOR_RBAC_MODE"] == "enforce"
