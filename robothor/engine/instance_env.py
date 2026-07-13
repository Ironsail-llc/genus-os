"""Load the instance's environment the way systemd does, for non-systemd callers.

The daemon's guardrail and feature-flag posture comes from the unit file:
an ``EnvironmentFile=`` plus the ``Environment=`` lines in the engine's
drop-in directory. Anything started *outside* systemd — ``robothor engine
run`` from a shell, an agent shelling out to the CLI, a cron script — inherits
only the caller's environment, so every rollout-gated guardrail read back as
its default (off/observe) while the daemon was enforcing.

That is a security hole, not a nuisance: a CLI run bypassed the exact controls
the daemon applied. This module reads the same two sources the unit does and
fills in what the caller did not set. An explicitly-set variable always wins,
so a deliberate override (or a test) still works.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the instance's config lives. Both are overridable so a non-standard
# deployment (or a test) can point elsewhere; the defaults match infra/systemd.
DEFAULT_ENV_FILE = Path(os.environ.get("ROBOTHOR_ENV_FILE", "/etc/robothor/robothor.env"))
DEFAULT_DROPIN_DIR = Path(
    os.environ.get(
        "ROBOTHOR_DROPIN_DIR",
        "/etc/systemd/system/robothor-engine.service.d",
    )
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (systemd EnvironmentFile format)."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = _unquote(value)
    return out


def _parse_dropins(dropin_dir: Path) -> dict[str, str]:
    """Parse ``Environment=KEY=VALUE`` lines from every live ``*.conf``.

    Only ``*.conf`` is live — systemd ignores everything else in the directory,
    and the operator's ``.bak-*`` copies of a drop-in must not leak stale
    guardrail modes back in.
    """
    out: dict[str, str] = {}
    for conf in sorted(dropin_dir.glob("*.conf")):
        for raw in conf.read_text().splitlines():
            line = raw.strip()
            if not line.startswith("Environment="):
                continue
            assignment = _unquote(line.removeprefix("Environment="))
            key, sep, value = assignment.partition("=")
            if sep:
                out[key.strip()] = _unquote(value)
    return out


def load_instance_env(
    *,
    env_file: Path | None = None,
    dropin_dir: Path | None = None,
) -> dict[str, str]:
    """Populate ``os.environ`` from the instance's systemd config.

    Never overrides a variable that is already set — the caller's explicit
    environment wins. Missing files are fine (a dev box has no ``/etc/robothor``).
    Returns the variables actually applied, for logging/tests.
    """
    env_file = env_file if env_file is not None else DEFAULT_ENV_FILE
    dropin_dir = dropin_dir if dropin_dir is not None else DEFAULT_DROPIN_DIR

    merged: dict[str, str] = {}
    for source, parse in ((env_file, _parse_env_file), (dropin_dir, _parse_dropins)):
        try:
            if source.exists():
                # drop-ins are layered over the env file, as systemd does
                merged.update(parse(source))
        except OSError as exc:
            logger.warning("could not read instance env from %s: %s", source, exc)

    applied = {k: v for k, v in merged.items() if k not in os.environ}
    os.environ.update(applied)
    return applied
