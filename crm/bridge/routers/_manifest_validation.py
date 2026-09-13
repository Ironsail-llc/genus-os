"""What can be said about one agent manifest, and what an EDIT is refused for.

Split out of ``agent_manifests.py``, which was the largest new file in the
change and the only one nothing capped. The cut is along a real seam rather
than at a line count: everything here is a verdict on a document, and nothing
here touches the filesystem, the engine or a request. That is what lets it take
its inputs as parameters — the manifest directory, the repo root, the fleet, the
engine's tool list — instead of importing the router's resolvers back, which
would be a cycle.

Two properties the whole module is written around:

* **A validator never takes a caller down.** ``manifest_schema.validate``
  documents "never raises" and ``manifest_checks.validate_agent`` implies it,
  and neither was true of an arbitrary document: ``v2.sandbox`` as a mapping
  hits ``x not in frozenset()`` on an unhashable, ``schedule`` as a list hits
  ``.get()``, a non-string ``id`` hits ``re.match``. Those are the manifests an
  operator needs the editor for MORE than a well-formed one, so a crash does
  not lose a verdict — it hides the file. :func:`guarded` wraps both.
* **A refusal names the manifest, not the box.** Findings carry dotted schema
  paths and stable codes. An exception's TEXT carries the offending manifest
  value, so only its TYPE comes out (CLAUDE.md rules 1 and 2).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: What every validator here answers: ``(blocking errors, advisory warnings)``.
IssueSplit: TypeAlias = tuple[list[dict[str, str]], list[dict[str, str]]]


@dataclass(frozen=True)
class ValidationContext:
    """One view of the world, shared by every verdict in a single request.

    An edit judges the document twice — as it was and as the edit leaves it —
    and the DIFFERENCE decides the refusal. Two reads of the engine's tool list
    across one save could disagree about which tools exist, and the disagreement
    would read as an error the operator had just introduced.
    """

    fleet: dict[str, Any]
    tools: set[str]
    manifest_dir: Path
    repo_root: Path
    tool_warnings: list[dict[str, str]] = field(default_factory=list)


def issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def not_validatable(where: str, error: BaseException) -> dict[str, str]:
    """A validator that raised, reported as a finding instead of a 500."""
    return issue(
        "",
        "not_validatable",
        f"{where} could not judge this manifest ({type(error).__name__}) — "
        "a value is the wrong shape",
    )


def guarded(where: str, call: Callable[[], IssueSplit]) -> IssueSplit:
    """Run one validator, turning a raise into an error finding.

    See the module docstring for why the two validators below cannot be trusted
    not to raise on an arbitrary document. ``runtime_issues`` already wrapped
    its own call for exactly this reason; this is the same guard at the others.
    """
    try:
        return call()
    except Exception as error:  # noqa: BLE001 — every shape of bad value lands here
        logger.warning("%s raised on a manifest: %s", where, type(error).__name__)
        return [not_validatable(where, error)], []


def schema_issues(document: dict[str, Any], manifest_dir: Path) -> IssueSplit:
    """``manifest_schema`` findings, split into blocking and advisory.

    Validated as the MERGED document — file plus ``_defaults.yaml`` — because
    that is what a run is judged on. Validating the fragment alone reports a
    missing model on every agent that correctly inherits one.
    """
    from robothor.engine import config as engine_config
    from robothor.engine import manifest_schema

    merged = engine_config._merged_manifest(
        document,
        agent_id=str(document.get("id") or ""),
        defaults=engine_config._load_defaults(manifest_dir),
        workspace=None,
        trigger_type=None,
    )
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for found in manifest_schema.validate(merged, strict=False):
        bucket = errors if found.severity == "error" else warnings
        bucket.append(issue(found.path, found.code, found.message))
    return errors, warnings


def check_issues(
    document: dict[str, Any], fleet: dict[str, Any], tools: set[str], repo_root: Path
) -> IssueSplit:
    """The 13 A–M checks, run server-side with ``ci=True``.

    ``ci=True`` skips the two checks (C, J) that assert files exist through
    symlinks the appliance may not have; the 422 decision is the remaining
    eleven plus the schema. A ``WARN`` never blocks — it is advice, and a
    surface that refuses to save on advice is one an operator learns to work
    around.
    """
    from robothor.templates.manifest_checks import validate_agent

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for result in validate_agent(document, fleet, tools, repo_root=repo_root, ci=True):
        if result.status not in {"FAIL", "WARN"}:
            continue
        detail = "; ".join([result.message, *result.details]).strip("; ")
        entry = issue(f"check.{result.check_id}", f"check_{result.check_id.lower()}", detail)
        (errors if result.status == "FAIL" else warnings).append(entry)
    return errors, warnings


def runtime_issues(document: dict[str, Any]) -> list[dict[str, str]]:
    """Can the engine actually turn this document into a scheduled agent?

    The two questions no static check answers: does ``manifest_to_agent_config``
    survive the document, and does APScheduler accept the cron in the timezone
    it is paired with. Both have been the difference between a manifest that
    validates and an agent that never fires.
    """
    from apscheduler.triggers.cron import CronTrigger

    from robothor.engine.config import manifest_to_agent_config

    issues: list[dict[str, str]] = []
    try:
        agent = manifest_to_agent_config(document)
    except Exception as error:  # noqa: BLE001 — every shape of bad value lands here
        return [issue("", "not_loadable", f"the engine cannot load this manifest: {error}")]

    crons = [("schedule.cron", agent.cron_expr, agent.timezone)]
    if agent.heartbeat:
        crons.append(("heartbeat.cron", agent.heartbeat.cron_expr, agent.heartbeat.timezone))
    if agent.worker:
        crons.append(("worker.cron", agent.worker.cron_expr, agent.worker.timezone))
    for path, cron, timezone in crons:
        if not cron:
            continue
        try:
            CronTrigger.from_crontab(cron, timezone=timezone)
        except Exception as error:  # noqa: BLE001 — bad cron, bad tz, both
            issues.append(issue(path, "bad_cron", f"the scheduler refuses this schedule: {error}"))
    return issues


async def validate(document: dict[str, Any], context: ValidationContext) -> dict[str, Any]:
    """Everything that can be said about one manifest, in one body.

    Ordered cheapest-first so the operator sees the structural complaint before
    the semantic one: a document missing ``id`` produces a readable answer
    rather than a cascade from every check that dereferences it.

    The context is required rather than fetched on demand: an implicit fetch
    meant a caller validating two documents silently paid for two engine round
    trips and risked two different answers. See :class:`ValidationContext`.
    """
    errors, warnings = await asyncio.to_thread(
        guarded, "the schema validator", lambda: schema_issues(document, context.manifest_dir)
    )
    check_errors, check_warnings = await asyncio.to_thread(
        guarded,
        "the manifest checks",
        lambda: check_issues(document, context.fleet, context.tools, context.repo_root),
    )
    errors.extend(check_errors)
    warnings.extend(check_warnings)
    warnings.extend(context.tool_warnings)
    errors.extend(runtime_issues(document))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def introduced(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The verdict on an EDIT: only the errors this edit is responsible for.

    "A save must not break a manifest" and "a save is gated on the manifest
    being unbroken" are different promises with opposite outcomes, and the
    second one locks the operator out of the file exactly when they need it.
    Running the full validator on the merged document made an agent with a bad
    cron or a tool the engine no longer registers impossible to repair AND
    impossible to `disable` — and `disable` is the stop control. Since the tool
    half reads the LIVE engine's registry, uninstalling one plugin froze every
    agent that named its tools. `DELETE` does not validate, so retiring the
    agent was the only remedy the Helm had left.

    Identity is ``(path, code, message)`` — the whole finding, not just where it
    landed. ``(path, code)`` alone was not enough: every ``manifest_checks``
    result collapses to one ``(check.D, check_d)`` pair whose MESSAGE
    enumerates the offending items, so adding a second unregistered tool to a
    manifest that already had one changed the message and nothing else, and the
    edit looked like it introduced nothing. A pre-existing fault is never a free
    pass for a second one of the same kind either; the message is where "the
    same kind" stops being the same complaint.

    Carried faults come back in ``warnings`` AND in ``pre_existing`` — the same
    findings under two keys, because a UI that wants to say "saved, but this
    agent is still broken for these reasons" should not have to diff two lists
    to work out which warnings were errors a moment ago.
    """

    def identity(one: dict[str, str]) -> tuple[str, str, str]:
        return (one["path"], one["code"], one["message"])

    already = {identity(one) for one in before["errors"]}
    new = [one for one in after["errors"] if identity(one) not in already]
    carried = [one for one in after["errors"] if identity(one) in already]
    return {
        "ok": not new,
        "errors": new,
        "warnings": [*after["warnings"], *carried],
        "pre_existing": carried,
    }
