"""``GET /api/logs`` — journald, for an operator who is not on the box.

Two routes, both operator-only:

``GET /api/logs/units``  which units may be followed, and whether journald is
                         reachable at all.
``GET /api/logs``        the last N lines of one of them.

**Why this is narrow on purpose.** Serving a shell's worth of log access over
HTTP is how a read-only page becomes command execution, so every degree of
freedom a caller has is enumerated here and nowhere else:

* the UNIT must be a member of the installed set — see :func:`unit_catalog`,
  which reads the unit files ``scripts/install-units.sh`` renders rather than a
  list somebody typed, so a unit added to ``infra/systemd`` is followable and
  ``sshd`` never is;
* ``lines`` is an integer in 1..1000;
* ``since`` must match a strict time pattern, and is then passed as a SINGLE
  ``--since=<value>`` token, so there is no argv position a caller can reach
  into even if the pattern were one day loosened;
* ``grep`` is never passed to journalctl at all. It is applied in Python.

**Why grep is applied AFTER redaction.** Filtering the raw line would make this
route an oracle: ``?grep=sk-or-abc123`` on a line whose output is redacted
would still report a hit, and a hit is the answer. So the substring test runs
against the same text the operator is shown, which is the text after
:func:`robothor.secrets.redaction.redact` and
:func:`robothor.sanitize.sanitize_log`.

**Why a missing journalctl is not an error.** In a container there is no
journald, and there is nothing broken about that. The routes answer
``available: false`` with a sentence saying why, so the page can say "logs are
not available on this deployment" instead of rendering a 500 that reads as an
appliance fault.

**No ``/api/logs/engine``.** The engine's health app (``robothor/engine/health.py``)
exposes no recent-log endpoint to proxy, and building one in the engine was out
of scope for this task. The engine's own journal is reachable here as the
``robothor-engine`` unit.

Handlers are plain ``def``: ``subprocess.run`` blocks, so FastAPI must run them
in its worker threadpool rather than on the event loop (see
``crm/bridge/tests/test_route_concurrency.py``).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from robothor.sanitize import sanitize_log
from robothor.secrets.redaction import redact
from routers._operator import require_operator
from routers._params import ISO_TIMESTAMP_PATTERN, positive_int

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])

#: Bound both calls: a wedged journalctl must not hold a threadpool worker.
JOURNAL_TIMEOUT_SECONDS = 10

MAX_LINES = 1000
DEFAULT_LINES = 200

#: Where ``scripts/install-units.sh`` puts the rendered units. This is the
#: authoritative answer on a real box — it is what is actually installed,
#: including anything installed since this file was written.
INSTALLED_UNIT_DIR = Path("/etc/systemd/system")

#: The templates the installer renders FROM, used when the directory above has
#: no robothor units (a dev checkout, or a container). ``crm/bridge/routers`` ->
#: repo root.
TEMPLATE_UNIT_DIR = Path(__file__).resolve().parents[3] / "infra" / "systemd"

#: Only ever a robothor unit, and never a template (``robothor-alert@``): an
#: instance-less template unit has no journal of its own, and the ``@`` would
#: be the one character in a unit name that means something else.
#:
#: Matched with ``fullmatch`` everywhere, never ``match``: ``$`` also matches
#: immediately before a trailing newline, so an anchored ``^…$`` still admits
#: ``"robothor-engine\n"``.
_UNIT_NAME = re.compile(r"robothor-[a-z0-9][a-z0-9-]*")

#: A relative age, in journald's units. This is the half of ``--since`` that
#: ``routers._params.ISO_TIMESTAMP_PATTERN`` cannot carry — a timestamp column
#: has no use for "1h" — and the only reason this route has a pattern of its
#: own rather than calling ``iso_timestamp``.
_RELATIVE_AGE = r"\d{1,6}[smhd]"

#: ``--since`` values this route will pass on: a relative age or an ISO-8601
#: date/time. Nothing else — not journald's own English ("yesterday", "2 hours
#: ago"), which is a parser this route does not need to expose.
#:
#: The newline case is not hypothetical. With ``^…$`` and ``.match()``,
#: ``since=1h\n`` passed validation, then failed the ``since[-1] in 'smhd'``
#: test below, lost its leading ``-`` and reached journald as an ABSOLUTE
#: timestamp — the request quietly answered a different question from the one
#: the operator asked.
_SINCE = re.compile(rf"(?:{_RELATIVE_AGE}|{ISO_TIMESTAMP_PATTERN})")

#: The journal fields this route reads. Named explicitly so a unit that logs a
#: 4MB structured record does not ship all of it to a browser.
_OUTPUT_FIELDS = "--output-fields=MESSAGE,PRIORITY,__REALTIME_TIMESTAMP"

# Bound at module level so a test can stand in for the subprocess at the
# router's own seam, rather than patching the `subprocess` module globally for
# everything else running in the same process.
_run = subprocess.run
_which = shutil.which


def _catalog_from(directory: Path) -> dict[str, str]:
    """``{unit name: Description=}`` for the robothor units in ``directory``."""
    catalog: dict[str, str] = {}
    try:
        paths = sorted(directory.glob("robothor-*.service"))
    except OSError:
        return catalog
    for path in paths:
        name = path.stem
        if not _UNIT_NAME.fullmatch(name):
            continue
        description = ""
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Description="):
                    description = line.split("=", 1)[1].strip()
                    break
        except OSError:
            description = ""
        catalog[name] = description
    return catalog


def unit_catalog() -> dict[str, str]:
    """Every unit this route will follow, and what each one is.

    Derived, never typed. The installed directory first, because that is what
    ``journalctl -u`` can actually answer for; the repo's templates as the
    fallback so a dev checkout and a container still render a useful page. Both
    are the same glob ``scripts/install-units.sh`` uses, which is what keeps a
    newly added unit from needing an edit here.
    """
    return _catalog_from(INSTALLED_UNIT_DIR) or _catalog_from(TEMPLATE_UNIT_DIR)


def _journalctl() -> tuple[str | None, str | None]:
    """``(path, None)`` when journald is usable here, else ``(None, reason)``."""
    path = _which("journalctl")
    if path is None:
        return None, "journald is not available on this deployment (journalctl is not installed)"
    try:
        probe = _run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=JOURNAL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return None, "journalctl timed out"
    except OSError as exc:
        logger.warning("journalctl probe failed: %s", sanitize_log(exc))
        return None, "journald is not available on this deployment (journalctl could not be run)"
    if probe.returncode != 0:
        return None, "journald is not available on this deployment (journalctl exited non-zero)"
    return path, None


def _timestamp(record: dict[str, Any]) -> str | None:
    """journald's ``__REALTIME_TIMESTAMP`` (microseconds) as ISO-8601."""
    from datetime import UTC, datetime

    raw = record.get("__REALTIME_TIMESTAMP")
    try:
        return datetime.fromtimestamp(int(str(raw)) / 1_000_000, tz=UTC).isoformat()
    except (TypeError, ValueError):
        return None


def _message(record: dict[str, Any]) -> str:
    """``MESSAGE``, safe to render.

    journald returns a list of byte values when the message is not valid UTF-8;
    the bytes are decoded rather than dropped, because "the log line that
    broke" is usually the one worth reading.
    """
    raw = record.get("MESSAGE", "")
    if isinstance(raw, list):
        try:
            raw = bytes(raw).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            raw = str(raw)
    # redact FIRST, sanitize second: the redactor's shapes are written against
    # ordinary text, and would have to learn `\x3d` to keep working on escaped
    # input. Sanitising afterwards still escapes everything a renderer cares
    # about, because the placeholder it is handed contains no control bytes.
    return sanitize_log(redact(str(raw)))


def _priority(record: dict[str, Any]) -> int | None:
    try:
        return int(str(record.get("PRIORITY")))
    except (TypeError, ValueError):
        return None


def _unavailable(unit: str | None, reason: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"available": False, "reason": reason}
    if unit is not None:
        payload.update({"unit": unit, "lines": [], "truncated": False})
    else:
        payload["units"] = []
    return payload


@router.get("/units")
def list_units(request: Request) -> dict[str, Any]:
    """Which units the operator may follow, and whether journald is here."""
    require_operator(request)
    catalog = unit_catalog()
    _, reason = _journalctl()
    if reason is not None:
        return _unavailable(None, reason)
    return {
        "units": [{"name": name, "description": catalog[name]} for name in sorted(catalog)],
        "available": True,
    }


@router.get("")
def read_logs(
    request: Request,
    unit: str = Query(..., description="An allowlisted robothor-* unit"),
    lines: str = Query(str(DEFAULT_LINES)),
    since: str | None = Query(None, description="1h / 30m / 7d, or an ISO-8601 timestamp"),
    grep: str | None = Query(None, description="Case-insensitive substring, applied in Python"),
) -> dict[str, Any]:
    """The last N lines of one unit's journal."""
    require_operator(request)
    catalog = unit_catalog()
    if unit not in catalog:
        # Naming the set is the whole value of the refusal: the alternative is
        # an operator guessing unit names against a 422 that will not say.
        raise HTTPException(
            status_code=422,
            detail="unknown unit; allowed units are " + ", ".join(sorted(catalog)),
        )
    count = positive_int(lines, field="lines", maximum=MAX_LINES)
    if since is not None and not _SINCE.fullmatch(since):
        raise HTTPException(
            status_code=422,
            detail="since must be a relative age (30m, 1h, 7d) or an ISO-8601 timestamp",
        )

    path, reason = _journalctl()
    if reason is not None or path is None:
        return _unavailable(unit, reason or "journald is not available on this deployment")

    argv = [path, "-u", unit, "-n", str(count), "-o", "json", "--no-pager", _OUTPUT_FIELDS]
    if since is not None:
        # ONE token, and journald's own spelling for a relative age is a
        # leading '-'. `--since=-1h` cannot be mistaken for a separate
        # argument the way `--since -1h` could.
        argv.append(f"--since={'-' + since if since[-1] in 'smhd' else since}")

    try:
        completed = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=JOURNAL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return _unavailable(unit, "timed out")
    except OSError as exc:
        logger.warning("journalctl read failed: %s", sanitize_log(exc))
        return _unavailable(unit, "journalctl could not be run")
    if completed.returncode != 0:
        # stderr is deliberately not returned: it is journald's, not ours, and
        # an operator reading someone else's error text is how a path or a
        # value that was never meant to be served gets served.
        logger.warning("journalctl exited %s for %s", completed.returncode, sanitize_log(unit))
        return _unavailable(unit, "journalctl could not read that unit's journal")

    entries: list[dict[str, Any]] = []
    for raw_line in (completed.stdout or "").splitlines():
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except ValueError:
            # One unparseable record is not a reason to fail the page.
            continue
        if not isinstance(record, dict):
            continue
        message = _message(record)
        if grep and grep.lower() not in message.lower():
            continue
        entries.append(
            {"ts": _timestamp(record), "priority": _priority(record), "message": message}
        )

    return {
        "unit": unit,
        "available": True,
        "lines": entries,
        # Measured against what journald RETURNED, not what survived ``grep``:
        # the question is whether the window was full, and a filter that
        # removed nine of ten lines does not make the window less full.
        "truncated": len((completed.stdout or "").splitlines()) >= count,
    }
