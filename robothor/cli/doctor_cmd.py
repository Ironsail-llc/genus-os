"""``genus doctor`` -- the command.

Thin by design: it turns flags into a :class:`~robothor.doctor.context.
DoctorContext`, hands them to the runner, and prints. Everything worth testing
is in :mod:`robothor.doctor`, which the bridge and a future install gate call
without going through argparse.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from robothor.doctor.context import DoctorContext
from robothor.doctor.render import render_json, render_text
from robothor.doctor.runner import DoctorReport, run_sync

if TYPE_CHECKING:  # pragma: no cover - typing only
    import argparse

__all__ = ["cmd_doctor", "run_doctor"]


def _load_instance_env() -> None:
    """Read ``<workspace>/genus.env`` before anything resolves settings.

    A compose instance keeps its only copy of the database password there --
    ``genus init`` generated it and the platform deliberately stores it nowhere
    else -- so without this the doctor reports ``db.connect`` failing against a
    database that is running perfectly well. The file is refused unless it is
    0600, and the refusal goes to stderr: stdout is a JSON contract.

    Never raises. This is the command an operator runs to diagnose a box whose
    settings do not even parse.
    """
    from robothor.secrets.env_file import apply_instance_env

    try:
        from robothor.settings import get_settings

        workspace = get_settings().paths.workspace
    except Exception:  # noqa: BLE001 - a box with no usable settings still gets checked
        return

    # Settings are not the only thing cached by now: `robothor.config` and the
    # connection pool were both built from an environment that did not have
    # this file's contents, and the database layer reads the config, not the
    # settings. `apply_instance_env` drops all three.
    result = apply_instance_env(workspace)
    if result.refused:
        print(f"genus doctor: {result.refused}", file=sys.stderr)


def run_doctor(args: argparse.Namespace) -> DoctorReport:
    """Build the context from parsed flags and run. Returns the report.

    Shared with ``genus config validate``, which is an alias for this command.
    """
    _load_instance_env()
    ctx = DoctorContext(
        timeout_s=float(getattr(args, "timeout", 5.0) or 5.0),
        dry_run=bool(getattr(args, "dry_run", False)),
        offline=bool(getattr(args, "offline", False)),
        fix=bool(getattr(args, "fix", False)),
    )
    return run_sync(
        ctx,
        only=getattr(args, "only", None),
        category=getattr(args, "category", None),
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run the checks and print them. Exit 0 healthy, 1 broken, 2 unrunnable."""
    report = run_doctor(args)
    if getattr(args, "json", False):
        print(render_json(report))
    else:
        print(render_text(report))
    if report.errored:
        # Also on stderr: a --json consumer piping stdout into jq would
        # otherwise see a well-formed document and no sign that nothing ran.
        print(f"genus doctor: {report.error_detail}", file=sys.stderr)
    return int(report.exit_code)
