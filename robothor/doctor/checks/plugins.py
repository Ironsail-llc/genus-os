"""Third-party code the engine imports, and whether the operator meant to.

The doctor asked about the database, the models, the host and the channels —
and nothing at all about the one part of an instance that runs code the
platform did not write. A plugin that stopped loading after an upgrade was
invisible until somebody noticed a capability missing; a manifest that changed
underneath a running engine was invisible full stop.

Three questions, and the severity of each is the difference between a fault and
a decision:

* ``plugins.lockfile`` — **recommended.** An instance with no lockfile works
  exactly as it always has. The file is what makes disabling and drift
  detection possible; it is not what makes the engine run, and failing an
  install gate over its absence would be a lie about what is broken.
* ``plugins.load`` — **required.** An installed plugin that is refused is a
  capability the operator believes they have and do not. The single refusal
  this accepts is ``disabled by operator``, because that is the operator's own
  decision arriving back at them.
* ``plugins.drift`` — **required.** A manifest that no longer hashes to what
  was recorded is the case ``verify_adapter_integrity`` exists for, one layer
  up. It deliberately overlaps ``plugins.load``: both go red, and this is the
  one that names ``genus plugin sync``.

Everything here runs through ``ctx.run_blocking``. Discovery walks the
installed-distribution metadata and the load path imports third-party modules,
neither of which belongs on the loop that is timing the check.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext


def _lockfile_state() -> dict[str, Any]:
    """Everything the lockfile check needs, read in one blocking pass."""
    from robothor.plugins.lockfile import read_lockfile

    lock = read_lockfile()
    state: dict[str, Any] = {
        "path": lock.path,
        "present": lock.present,
        "malformed": lock.malformed,
        "rows": len(lock.rows),
        "disabled": sum(1 for row in lock.rows.values() if not row.enabled),
        "mode": None,
    }
    if lock.path is not None and lock.present:
        try:
            state["mode"] = stat.S_IMODE(lock.path.stat().st_mode)
        except OSError:
            state["mode"] = None
    return state


async def _lockfile(ctx: DoctorContext) -> Result:
    """The plugin lockfile exists, parses, and is readable only by its owner."""
    state = await ctx.run_blocking(_lockfile_state)
    if state["path"] is None:
        return skip("no workspace resolves, so no lockfile path does either")
    if not state["present"]:
        return fail(
            "no plugin lockfile — every installed plugin loads unconditionally "
            "and nothing would notice a manifest changing. Run `genus plugin sync`."
        )
    if state["malformed"]:
        return fail(
            "the plugin lockfile does not parse as JSON, so it is being ignored "
            "and every installed plugin loads unconditionally. Run "
            "`genus plugin sync` to rewrite it."
        )
    mode = state["mode"]
    if mode is not None and mode & 0o077:
        return fail(
            f"the plugin lockfile is mode {mode:o} — it records what this "
            "instance runs and must be 0600. Run `genus plugin sync` to rewrite it."
        )
    return ok(f"{state['rows']} plugin(s) recorded, {state['disabled']} disabled")


def _load_state() -> list[dict[str, Any]]:
    from robothor.plugins.inventory import inventory

    return [
        {
            "name": row.name,
            "state": row.state,
            "reason": row.failure_reason,
            "drifted": row.drifted,
            "recorded": row.recorded,
        }
        for row in inventory()
    ]


async def _load(ctx: DoctorContext) -> Result:
    """Every installed plugin either loaded or is disabled on purpose."""
    try:
        rows = await ctx.run_blocking(_load_state)
    except Exception as error:  # noqa: BLE001 - a broken registry is a skip, not a crash
        return skip(f"plugin discovery failed ({type(error).__name__})")

    if not rows:
        return ok("no plugins installed")
    refused = [row for row in rows if row["state"] == "failed"]
    disabled = [row["name"] for row in rows if row["state"] == "disabled"]
    if refused:
        detail = "; ".join(f"{row['name']}: {row['reason']}" for row in refused)
        return fail(f"{len(refused)} installed plugin(s) refused — {detail}")
    loaded = sum(1 for row in rows if row["state"] == "loaded")
    note = (
        f", {len(disabled)} disabled on purpose ({', '.join(sorted(disabled))})" if disabled else ""
    )
    return ok(f"{loaded} plugin(s) loaded{note}")


async def _drift(ctx: DoctorContext) -> Result:
    """No recorded plugin's manifest has changed since it was recorded."""
    try:
        rows = await ctx.run_blocking(_load_state)
    except Exception as error:  # noqa: BLE001 - a broken registry is a skip, not a crash
        return skip(f"plugin discovery failed ({type(error).__name__})")

    recorded = [row for row in rows if row["recorded"]]
    if not recorded:
        return ok("nothing recorded, so nothing to compare")
    drifted = sorted(row["name"] for row in recorded if row["drifted"])
    if drifted:
        return fail(
            f"{len(drifted)} plugin(s) whose manifest no longer matches what was "
            f"recorded: {', '.join(drifted)}. They are refused rather than "
            "imported. Review what changed, then `genus plugin sync`."
        )
    return ok(f"{len(recorded)} recorded plugin(s) match their manifest")


CHECKS: tuple[Check, ...] = (
    Check(
        id="plugins.lockfile",
        title="The plugin lockfile is present, parseable and private",
        category="plugins",
        severity="recommended",
        run=_lockfile,
    ),
    Check(
        id="plugins.load",
        title="Every installed plugin loaded, or is disabled on purpose",
        category="plugins",
        severity="required",
        run=_load,
    ),
    Check(
        id="plugins.drift",
        title="No plugin's manifest changed since it was recorded",
        category="plugins",
        severity="required",
        run=_drift,
    ),
)
