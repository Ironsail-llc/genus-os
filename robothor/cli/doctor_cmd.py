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


def run_doctor(args: argparse.Namespace) -> DoctorReport:
    """Build the context from parsed flags and run. Returns the report.

    Shared with ``genus config validate``, which is an alias for this command.
    """
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
