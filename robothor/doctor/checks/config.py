"""Does this instance's configuration load, and does it say what it looks like?

Four ways a config file lies, in the order they cost an operator time:

1. it does not parse at all, so nothing starts;
2. it holds a key nothing reads -- a typo that looks deliberate;
3. it uses a name that has been replaced, and is being read through a fallback;
4. it disagrees with the environment the running process was started from, so
   an edit appears to have had no effect.

``genus config validate`` reported 2-4 and is now an alias for this command;
this module is where that logic lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]


async def _settings_load(ctx: DoctorContext) -> Result:
    """The settings model builds from this box's config.yaml and environment.

    A failure here means NOTHING will start: the engine, the bridge and the CLI
    all resolve settings the same way. The message names the offending key.
    Under ``ROBOTHOR_CONFIG_STRICT_MODE=enforce`` an unknown key in
    config.yaml lands here rather than under ``config.unknown_keys``, which is
    the difference between "will not boot" and "is carrying a typo".
    """

    def _load() -> str:
        from robothor.settings import get_settings

        settings = get_settings()
        return str(settings.paths.workspace)

    try:
        workspace = await ctx.run_blocking(_load)
    except Exception as exc:  # noqa: BLE001 - reporting it IS this check
        return fail(f"settings do not resolve: {type(exc).__name__}: {exc}")
    return ok(f"settings resolved; workspace {workspace}")


async def _unknown_keys(ctx: DoctorContext) -> list[Result]:
    """config.yaml holds a key no setting declares, so nothing reads it.

    Almost always a typo (``max_concurent_agents``), which is worse than an
    error because it looks applied. Recommended rather than required while
    ``ROBOTHOR_CONFIG_STRICT_MODE`` is ``observe``; in ``enforce`` the same key
    stops the process and is reported by ``config.settings_load`` instead.
    """

    def _scan() -> tuple[list[str], str, str]:
        from robothor.settings.sources import config_yaml_path, strict_mode, unknown_config_keys

        return list(unknown_config_keys()), strict_mode(), str(config_yaml_path() or "")

    try:
        unknown, mode, path = await ctx.run_blocking(_scan)
    except ValueError as exc:  # a malformed file names itself
        return [fail(str(exc))]
    if not unknown:
        return [ok(f"no unknown keys{f' in {path}' if path else ''}")]
    return [
        fail(f"{key} is set in config.yaml but nothing reads it (mode={mode})", sub_id=key)
        for key in unknown
    ]


async def _deprecated_aliases(ctx: DoctorContext) -> list[Result]:
    """A setting is being read through a name that has been replaced.

    The old name still works and will for two more minor releases. Until it is
    changed, two halves of the platform can disagree about the same value --
    which is why the replacement exists. Each line names the old name and the
    new one.
    """

    def _scan() -> list[tuple[str, str]]:
        from robothor.settings import provenance

        return list(provenance.deprecated_names_in_use())

    in_use = await ctx.run_blocking(_scan)
    if not in_use:
        return [ok("no deprecated configuration names in use")]
    return [
        fail(f"{old} is deprecated and still set; use {new}", sub_id=old) for old, new in in_use
    ]


async def _pending_restart(ctx: DoctorContext) -> list[Result]:
    """config.yaml and the running process disagree about a restart-required
    setting.

    The environment wins over the file -- documented precedence, not a broken
    instance -- so this is never fatal. It is reported because an operator who
    edited config.yaml and saw nothing change otherwise has no way to find out
    why: the variable is still set in ``/etc/robothor/robothor.env`` or in the
    unit, and the service has to be restarted after it is cleared. Secret
    fields are reported as ``<set>`` on both sides.
    """

    def _scan() -> list[tuple[str, str]]:
        from robothor.cli.config_cmd import _agree, _units_for
        from robothor.settings import provenance
        from robothor.settings.registry import field_index

        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for record in field_index().values():
            if record["env"] in seen or not record["restart_required"]:
                continue
            seen.add(record["env"])
            present, raw = provenance.file_value(record)
            if not present:
                continue
            env_name = provenance.env_name_in_use(record)
            if env_name is None:
                continue
            running = provenance.running_env_value(env_name) or ""
            if _agree(record, raw, running):
                continue
            shown_file = "<set>" if record["secret"] else repr(raw)
            shown_env = "<set, differing>" if record["secret"] else repr(running)
            rows.append(
                (
                    record["env"],
                    f"config.yaml says {shown_file}, this process reads {shown_env} from "
                    f"{env_name} (the environment wins) — clear the variable and restart "
                    f"{', '.join(_units_for(record)) or 'the reader'} to apply the file",
                )
            )
        return rows

    rows = await ctx.run_blocking(_scan)
    if not rows:
        return [ok("no setting is waiting on a restart")]
    return [fail(detail, sub_id=name) for name, detail in rows]


def _benchmark_tool_names() -> set[str]:
    """Tool names that RUN the benchmark harness, read off the harness module.

    Derived, not typed out: a hand-written list of agent ids ("benchmark-runner"
    and whatever the operator called theirs) is the drift that produced three
    separate hardcoded-name defects on this instance. An agent that can call
    ``benchmark_run_fleet`` schedules benchmarks, whatever it is named.
    """
    from robothor.engine.tools.handlers.benchmark import HANDLERS

    return set(HANDLERS)


def _agent_schedules_benchmarks(manifest: dict[str, Any]) -> bool:
    if manifest.get("is_benchmark"):
        return True
    allowed = manifest.get("tools_allowed") or []
    return bool(set(map(str, allowed)) & _benchmark_tool_names())


async def _benchmark_isolation(ctx: DoctorContext) -> Result:
    """A scheduled benchmark runs against this instance's own tenant.

    ``ROBOTHOR_BENCHMARK_SANDBOX_MODE=off`` means benchmark sub-runs are never
    scoped to the dedicated ``benchmark-sandbox`` tenant: the harness executes
    each graded task as a child run under the GRADED AGENT's tenant, which on a
    single-tenant instance is the operator's own. Every durable write from such
    a child is refused at the boundary since 2026-09-12 -- and "refused at the
    boundary" is one control, deliberately the last one, not a design.

    The combination this reports is a schedule plus an off sandbox. Recommended
    rather than required because the instance works: what it does not do is
    give the graded agent a real place to act, so every rubric that grades an
    action can only be satisfied by narrating one.
    """

    def _scan() -> tuple[str, list[str]]:
        from robothor.engine.feature_flags import benchmark_sandbox_mode

        mode = str(benchmark_sandbox_mode())
        if mode != "off":
            return mode, []

        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT agent_id FROM agent_schedules WHERE enabled")
            scheduled = [str(row[0]) for row in cursor.fetchall()]

        import yaml

        from robothor.engine.config import EngineConfig

        manifest_dir = EngineConfig.from_env().manifest_dir
        graders: list[str] = []
        for agent_id in sorted(scheduled):
            path = manifest_dir / f"{agent_id}.yaml"
            if not path.is_file():
                # A schedule outliving its manifest is manifests.missing's
                # finding, not evidence that this instance benchmarks.
                continue
            try:
                manifest = yaml.safe_load(path.read_text()) or {}
            except Exception:  # noqa: BLE001 - manifests.broken owns unreadable files
                continue
            if isinstance(manifest, dict) and _agent_schedules_benchmarks(manifest):
                graders.append(agent_id)
        return mode, graders

    try:
        mode, graders = await ctx.run_blocking(_scan)
    except Exception as exc:  # noqa: BLE001 - an unreachable database is not a verdict
        return skip(f"cannot read agent_schedules: {type(exc).__name__}: {exc}")

    if mode != "off":
        return ok(f"benchmark sandbox mode={mode}; graded runs are scoped to the sandbox tenant")
    if not graders:
        return ok("benchmark sandbox is off, and no enabled schedule runs the benchmark harness")
    return fail(
        f"{', '.join(graders)} {'is' if len(graders) == 1 else 'are'} scheduled to run "
        "benchmarks while ROBOTHOR_BENCHMARK_SANDBOX_MODE is off, so graded sub-runs "
        "execute under this instance's own tenant. Set "
        "ROBOTHOR_BENCHMARK_SANDBOX_ENABLED=1 and ROBOTHOR_BENCHMARK_SANDBOX_MODE=observe, "
        "or disable the schedule. See docs/runbooks/BENCHMARK_SANDBOX.md."
    )


CHECKS: tuple[Check, ...] = (
    Check(
        id="config.settings_load",
        title="Settings resolve",
        category="config",
        severity="required",
        run=_settings_load,
    ),
    Check(
        id="config.unknown_keys",
        title="config.yaml holds only keys something reads",
        category="config",
        severity="recommended",
        run=_unknown_keys,
    ),
    Check(
        id="config.deprecated_aliases",
        title="No deprecated setting names in use",
        category="config",
        severity="recommended",
        run=_deprecated_aliases,
    ),
    Check(
        id="config.pending_restart",
        title="No setting is waiting on a restart",
        category="config",
        severity="recommended",
        run=_pending_restart,
    ),
    Check(
        id="benchmark.isolation",
        title="No benchmark is scheduled against this instance's own tenant",
        category="config",
        severity="recommended",
        run=_benchmark_isolation,
    ),
)
