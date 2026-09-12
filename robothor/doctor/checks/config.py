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

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok

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
)
