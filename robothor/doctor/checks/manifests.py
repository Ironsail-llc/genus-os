"""Agent manifests: do they parse, and does the schema accept them?

On 2026-08-24 a YAML typo in one manifest deleted the fleet's primary agent for
three hours and 48 minutes. The file was unreadable, it was silently dropped,
and ``reconcile_schedules`` then pruned the schedules of an agent it could no
longer see. Four guards existed; none of them ran. These two checks are the
question those guards were supposed to answer, asked from outside the engine.

Nothing here puts manifest CONTENT in a result. A manifest is instruction text
written for a model, and a doctor that echoed it into a terminal, a log and an
operator dashboard would be carrying attacker-controlled prose across three
trust boundaries. Results carry the agent id, the key path and the issue code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

#: How many offending lines a single result names before it summarises. Longer
#: than this and the terminal output stops being readable, which makes the
#: whole report less useful than a shorter one.
_MAX_LISTED = 8


def _manifest_dir(ctx: DoctorContext) -> Path:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().manifest_dir


def _scan_schema(directory: Path) -> tuple[int, list[str], list[str]]:
    """``(agents, error lines, warning lines)`` from a strict validation pass.

    Two things this does the same way the ENGINE does, because a doctor whose
    verdict differs from the loader's is worse than no doctor:

    * **a file with no ``id`` is not an agent.** ``_defaults.yaml`` and
      ``schema.yaml`` live in the same directory; judging them as manifests
      reports five missing required fields per file for documents that were
      never meant to have them.
    * **what is validated is the MERGED document**, defaults plus manifest,
      which is what ``_enforce_manifest_schema`` judges and therefore what
      ``enforce`` would actually refuse. Validating the raw file reports
      errors the engine would not, and misses a typo in ``_defaults.yaml``
      that breaks every agent inheriting it.
    """
    import yaml

    from robothor.engine import config as engine_config
    from robothor.engine import manifest_schema

    errors: list[str] = []
    warnings: list[str] = []
    agents = 0
    defaults = engine_config._load_defaults(directory)
    for path in sorted(directory.glob("*.yaml")):
        try:
            data: Any = yaml.safe_load(path.read_text())
        except Exception as exc:  # noqa: BLE001 - manifests.broken owns unreadable files
            errors.append(f"{path.stem}: unreadable ({type(exc).__name__})")
            continue
        if not isinstance(data, dict) or "id" not in data:
            continue
        agents += 1
        agent_id = str(data.get("id") or path.stem)
        merged = engine_config._merged_manifest(
            data, agent_id=agent_id, defaults=defaults, workspace=None, trigger_type=None
        )
        for issue in manifest_schema.validate(merged, strict=True):
            line = f"{agent_id}: {issue.path} [{issue.code}]"
            (errors if issue.severity == "error" else warnings).append(line)
    return agents, errors, warnings


def _summarise(kind: str, lines: list[str]) -> str:
    shown = "; ".join(lines[:_MAX_LISTED])
    if len(lines) > _MAX_LISTED:
        shown += f"; and {len(lines) - _MAX_LISTED} more"
    return f"{len(lines)} schema {kind}(s): {shown}"


async def _schema(ctx: DoctorContext) -> Result:
    """Every agent manifest satisfies the shipped schema, strictly.

    An error means the manifest carries a key the schema does not define or a
    value of the wrong type. Under ``ROBOTHOR_MANIFEST_SCHEMA_MODE=enforce``
    that agent will not load at all; under ``observe`` -- the default -- it
    loads with the offending key doing nothing, which looks deliberate and is
    how a typo survives review. Each line names the agent and the key path,
    never the value or the instruction text.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    try:
        files, errors, _warnings = await ctx.run_blocking(_scan_schema, directory)
    except Exception as exc:  # noqa: BLE001 - an unreadable dir is a result
        return fail(f"cannot read the manifest directory: {type(exc).__name__}")
    if errors:
        return fail(_summarise("error", errors))
    return ok(f"{files} manifest(s) validate strictly")


async def _schema_warnings(ctx: DoctorContext) -> Result:
    """A manifest uses a key the schema has deprecated.

    It still works. It will stop working, and until it is changed the manifest
    describes the fleet in a vocabulary the platform is retiring. Its own check
    rather than a second line under ``manifests.schema`` because severity is a
    property of the check: an error stops an agent loading and a deprecation
    does not, and folding them together would either make a deprecation fatal
    or make a schema error advisory.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    try:
        _files, _errors, warnings = await ctx.run_blocking(_scan_schema, directory)
    except Exception as exc:  # noqa: BLE001 - an unreadable dir is a result
        return fail(f"cannot read the manifest directory: {type(exc).__name__}")
    if warnings:
        return fail(_summarise("warning", warnings))
    return ok("no deprecated manifest keys in use")


async def _broken(ctx: DoctorContext) -> Result:
    """No manifest file is present-but-unreadable.

    This is the 2026-08-24 outage exactly: a file that exists and will not
    parse is not an absent agent, and treating it as one lets the scheduler
    prune a live agent's cron. Any failure here means an agent the fleet
    believes it has is not running, and the fix is the file named in the
    detail.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")

    def _scan() -> tuple[int, list[str]]:
        from robothor.engine.config import load_manifest_dir

        scan = load_manifest_dir(directory)
        return len(scan.manifests), [
            f"{failure.agent_id or failure.filename}: {failure.error_type}"
            for failure in scan.failures
        ]

    try:
        loaded, failures = await ctx.run_blocking(_scan)
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifest directory: {type(exc).__name__}")
    if failures:
        return fail(f"{len(failures)} unreadable manifest(s): " + "; ".join(failures[:_MAX_LISTED]))
    return ok(f"{loaded} manifest(s) loaded, none broken")


async def _unrestricted_role(ctx: DoctorContext) -> Result:
    """Every agent that runs with the unrestricted default role, by id.

    An agent that declares no ``role`` resolves to the default service role,
    which the shipped permissions seed as allow-all: RBAC evaluates it and
    permits every tool. Each agent is resolved the way the engine resolves it
    (fleet defaults and ``ROBOTHOR_DEFAULT_SERVICE_ROLE`` included). A warning,
    not a defect: the instance works, but its role controls restrict nothing
    for these agents. Nothing is changed.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")

    def _scan() -> tuple[int, list[str]]:
        from robothor.engine.config import load_agent_config, load_manifest_dir
        from robothor.engine.service_roles import ALLOW_ALL_ROLE

        ids = [str(m.get("id")) for m in load_manifest_dir(directory).manifests if m.get("id")]
        unrestricted = []
        for agent_id in ids:
            try:
                config = load_agent_config(agent_id, directory)
            except Exception:  # noqa: BLE001, S112 - manifests.schema owns refusals
                continue
            if config is not None and config.service_role == ALLOW_ALL_ROLE:
                unrestricted.append(agent_id)
        return len(ids), sorted(unrestricted)

    try:
        total, unrestricted = await ctx.run_blocking(_scan)
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifest directory: {type(exc).__name__}")
    if unrestricted:
        shown = ", ".join(unrestricted[:_MAX_LISTED])
        if len(unrestricted) > _MAX_LISTED:
            shown += f", and {len(unrestricted) - _MAX_LISTED} more"
        return fail(
            f"{len(unrestricted)} of {total} agent(s) run with the unrestricted default role "
            f"(every tool allowed): {shown}. Set `role:` in each manifest, or move the "
            "fleet with ROBOTHOR_DEFAULT_SERVICE_ROLE"
        )
    return ok(f"{total} agent(s), none on the unrestricted default role")


def _missing_prompt_files(directory: Path, workspace: Path, only: str) -> tuple[int, list[str]]:
    """``(agents, "agent: path" lines)`` for every instruction or bootstrap file absent.

    Paths are judged the way ``build_system_prompt`` reads them: relative to
    the workspace. Only the agent id and the path the manifest names are
    reported, never file content.
    """
    from robothor.engine.config import load_agent_config, load_manifest_dir

    ids = [str(m.get("id")) for m in load_manifest_dir(directory).manifests if m.get("id")]
    if only:
        ids = [i for i in ids if i == only]
    missing: list[str] = []
    for agent_id in ids:
        try:
            config = load_agent_config(agent_id, directory)
        except Exception:  # noqa: BLE001, S112 - manifests.schema owns refusals
            continue
        if config is None:
            continue
        paths = [config.instruction_file] if config.instruction_file else []
        paths.extend(config.bootstrap_files or [])
        missing.extend(f"{agent_id}: {rel}" for rel in paths if not (workspace / rel).exists())
    return len(ids), missing


async def _prompt_files(ctx: DoctorContext) -> Result:
    """Every manifest's ``instruction_file`` and ``bootstrap_files`` exist.

    The engine skips a missing one and keeps going, so the agent runs without
    the text it was meant to have, and its instructions may tell it to read
    files that are not there either. Each line names the agent and the path the
    manifest gives; create the file or correct the path.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    try:
        total, missing = await ctx.run_blocking(
            _missing_prompt_files, directory, ctx.workspace, ctx.agent
        )
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifest directory: {type(exc).__name__}")
    if missing:
        shown = "; ".join(missing[:_MAX_LISTED])
        if len(missing) > _MAX_LISTED:
            shown += f"; and {len(missing) - _MAX_LISTED} more"
        return fail(
            f"{len(missing)} instruction/bootstrap file(s) missing (paths are relative to "
            f"the workspace): {shown}"
        )
    return ok(f"{total} agent(s), every instruction and bootstrap file present")


CHECKS: tuple[Check, ...] = (
    Check(
        id="manifests.schema",
        title="Manifests satisfy the schema",
        category="manifests",
        severity="required",
        run=_schema,
    ),
    Check(
        id="manifests.schema_warnings",
        title="No deprecated manifest keys",
        category="manifests",
        severity="recommended",
        run=_schema_warnings,
    ),
    Check(
        id="manifests.broken",
        title="No manifest is present-but-unreadable",
        category="manifests",
        severity="required",
        run=_broken,
    ),
    Check(
        id="manifests.prompt_files",
        title="Every instruction and bootstrap file exists",
        category="manifests",
        severity="required",
        run=_prompt_files,
    ),
    Check(
        id="manifests.unrestricted_role",
        title="No agent runs with the unrestricted default role",
        category="manifests",
        severity="recommended",
        run=_unrestricted_role,
    ),
)
