"""Does the box match the repo?

``scripts/instance_doctor.sh`` has been able to answer this since the day it
was written, and nothing has ever read its output: it prints ``FINDING [CODE]
message`` lines from a timer, into a journal. The findings it catches are real
and invisible from anywhere else -- a timer symlinked into a checkout, nine
live units with no template, hand-written drop-ins with no mirror, a flag set
in both ``robothor.env`` and a drop-in where the env file silently wins.

This check does not re-implement any of it. It runs the script, parses the
lines, and turns each one into a row of the report. On a container or a Helm
pod, where there is no systemd and no ``/etc/systemd/system`` to compare
against, it skips and says so rather than reporting a host that does not exist.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS", "parse_findings", "running_under_systemd"]

#: The script's own line format. Anything else on stdout -- section headers,
#: indented detail, the summary -- is deliberately ignored: a parser that tried
#: to interpret the prose would break the first time the script's wording
#: changed, and the FINDING line is the part that is a contract.
_FINDING_PREFIX = "FINDING "

#: The script walks every unit and drop-in on the box and shells out per file,
#: so it is slower than a probe. Given more than the per-check budget it is
#: reported as a timeout like anything else; given its own generous budget it
#: would be the one check that can make ``genus doctor`` feel hung.
_SCRIPT = "scripts/instance_doctor.sh"


def running_under_systemd() -> bool:
    """Whether this process is on a host with a live systemd.

    ``/run/systemd/system`` is systemd's own documented marker: it exists if
    and only if systemd is the init system of the running host. A container
    started from the same image has neither it nor the units the script
    compares against.
    """
    return Path("/run/systemd/system").is_dir()


def parse_findings(output: str) -> list[tuple[str, str]]:
    """``(code, message)`` for every ``FINDING [CODE] message`` line."""
    findings: list[tuple[str, str]] = []
    for line in output.splitlines():
        text = line.strip()
        if not text.startswith(_FINDING_PREFIX):
            continue
        body = text[len(_FINDING_PREFIX) :].strip()
        code = ""
        if body.startswith("["):
            code, _, body = body[1:].partition("]")
        findings.append((code.strip() or "FINDING", body.strip()))
    return findings


def _script_path(workspace: Path) -> Path | None:
    """The host script, in the workspace checkout or on PATH."""
    candidate = workspace / _SCRIPT
    if candidate.is_file():
        return candidate
    found = shutil.which("instance_doctor.sh")
    return Path(found) if found else None


async def _unit_drift(ctx: DoctorContext) -> list[Result]:
    """The installed systemd units match what the repo would render.

    Each finding is one way the box and the checkout disagree: a unit with no
    template (so an upgrade will never touch it), a drop-in that is not
    mirrored, a rendered unit that has been edited in place, a service enabled
    but not running, or a variable set in two places where one silently wins.
    None of these stops the instance today; every one of them makes the next
    upgrade or the next reboot unpredictable, which is why they are
    recommended rather than required.

    Skipped where there is no systemd -- a container, a Helm pod -- because
    there is no host to compare.
    """
    if not await ctx.run_blocking(running_under_systemd):
        return [skip("not running under systemd (container or Helm); no host units to compare")]

    script = _script_path(ctx.workspace)
    if script is None:
        return [skip(f"{_SCRIPT} is not present in this installation")]

    def _run() -> tuple[int, str]:
        completed = subprocess.run(  # noqa: S603 - fixed argv, path resolved above
            [str(script)],
            capture_output=True,
            text=True,
            timeout=ctx.timeout_s,
            check=False,
        )
        return completed.returncode, completed.stdout

    try:
        code, output = await ctx.run_blocking(_run)
    except subprocess.TimeoutExpired:
        return [fail(f"{_SCRIPT} did not finish within {ctx.timeout_s:g}s")]
    except Exception as exc:  # noqa: BLE001 - a script that will not run is a result
        return [fail(f"{_SCRIPT} could not be run: {type(exc).__name__}")]

    findings = parse_findings(output)
    if findings:
        # The script emits one line per offending FILE, so a code repeats as
        # often as there are files. A repeated row id would make two findings
        # look like one to anything keying on it, so the second and later
        # occurrences carry an ordinal.
        seen: dict[str, int] = {}
        rows: list[Result] = []
        for code_name, message in findings:
            seen[code_name] = seen.get(code_name, 0) + 1
            suffix = "" if seen[code_name] == 1 else f".{seen[code_name]}"
            rows.append(fail(message, sub_id=f"{code_name}{suffix}"))
        return rows
    if code not in (0, 1):
        return [fail(f"{_SCRIPT} exited {code} without reporting a finding")]
    return [ok("the installed units match the repo (0 findings)")]


CHECKS: tuple[Check, ...] = (
    Check(
        id="host.unit_drift",
        title="Installed units match the repo",
        category="host",
        severity="recommended",
        run=_unit_drift,
    ),
)
