"""``genus plugin`` — what is installed, and what the engine will do with it.

The seam shipped with ten entry-point groups and no operator surface at all:
nothing told you what was installed, what it contributed, or why something was
refused. ``list`` closed the first half of that. The rest of this module closes
the second: there was no way to turn an installed plugin OFF short of
uninstalling the distribution, and no record of what had been accepted.

``list`` still matters most for the manifest ladder.
``ROBOTHOR_PLUGIN_MANIFEST_MODE`` defaults to ``observe`` because requiring a
``genus-plugin.yaml`` is a breaking change for anything published before it
existed — and an operator deciding whether to promote to ``enforce`` needs to
know which installed distributions would stop loading.

Two rules run through every verb here:

* **Nothing in this module signals the engine.** ``enable`` and ``disable``
  write a file; the daemon is still serving the set it discovered at boot until
  somebody reloads it. Every mutation says so, because a control an operator
  believes has already applied is worse than no control.
* **An unknown name is exit 2, never a new row.** A ``disable`` that invented a
  row for a typo would report success and change nothing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import argparse

__all__ = ["cmd_plugin", "cmd_plugin_install", "cmd_plugin_list", "cmd_plugin_remove"]

#: Printed by every verb that writes the lockfile. The engine re-reads it on
#: SIGHUP (``robothor/engine/daemon.py``) or on ``POST /api/plugins/reload``.
_RELOAD_HINT = "reload the engine (SIGHUP) or restart to apply"


def _mode_header() -> None:
    from robothor.plugins.manifest import MANIFEST_NAME, manifest_mode

    mode = manifest_mode()
    print(f"manifest mode: {mode}")
    if mode != "enforce":
        print(
            f"  NOTE: {mode} still IMPORTS a distribution that ships no "
            f"{MANIFEST_NAME}. The refuse-before-import guarantee applies only "
            "in enforce."
        )
    print()


def _lock_header() -> None:
    from robothor.plugins.lockfile import read_lockfile

    lock = read_lockfile()
    if lock.malformed:
        print(
            "lockfile: unreadable — every installed plugin loads as if there "
            "were none. Run `genus plugin sync` to rewrite it."
        )
    elif not lock.present:
        print("lockfile: none yet. `genus plugin sync` records what is installed.")
    else:
        disabled = sum(1 for row in lock.rows.values() if not row.enabled)
        print(f"lockfile: {len(lock.rows)} recorded, {disabled} disabled")
    print()


def _print_inventory() -> None:
    from robothor.plugins.inventory import inventory

    rows = inventory()
    if not rows:
        return
    print("Installed:")
    for row in rows:
        flag = "enabled" if row.enabled else "disabled"
        state = row.state if row.state != "loaded" else "loaded"
        print(f"  {row.name} {row.version or '-'}  {flag}  {state}  verdict={row.verdict or '-'}")
        if row.groups:
            print(f"      groups       {', '.join(row.groups)}")
        if row.contributions:
            shape = ", ".join(f"{kind}:{count}" for kind, count in row.contributions.items())
            print(f"      contributes  {shape}")
        if row.failure_reason:
            print(f"      not loaded   {row.failure_reason}")
    print()


def cmd_plugin_list() -> int:
    """Print installed plugins, what they contribute, and any refusals."""
    from robothor.plugins.loader import load_plugins

    _mode_header()
    _lock_header()
    result = load_plugins()

    contributions: dict[str, list[str]] = {}
    for kind in ("tools", "schemas", "services", "guardrails", "hooks", "models", "jobs"):
        names = sorted(getattr(result, kind, {}) or {})
        if names:
            contributions[kind] = names

    if not result.loaded and not result.failures:
        print("No plugins installed.")
        print("  Genus discovers extensions through Python entry points; see")
        print("  docs/PLUGINS.md and plugins/genus-hostinfo for a worked example.")
        return 0

    _print_inventory()

    if contributions:
        print("Loaded:")
        for kind, names in contributions.items():
            print(f"  {kind:<11} {', '.join(names)}")
    else:
        print("Loaded: nothing (every entry point was refused — see below)")

    # A deliberate disable is not a refusal, and printing it under a heading
    # that ends "fix the cause or uninstall the distribution" told an operator
    # their own decision was a fault they should undo.
    from robothor.plugins.lockfile import DISABLED_REASON

    turned_off = [f for f in result.failures if f.reason == DISABLED_REASON]
    refused = [f for f in result.failures if f.reason != DISABLED_REASON]

    if turned_off:
        print()
        print("Disabled:")
        for f in turned_off:
            print(f"  {f.name} [{f.group}]: {DISABLED_REASON}")
        print()
        print("  Turn one back on with `genus plugin enable <name>` (the")
        print("  DISTRIBUTION name — `genus plugin list` shows it above).")

    if refused:
        print()
        print("Refused:")
        for f in refused:
            print(f"  {f.name} [{f.group}]: {f.reason}")
        print()
        print(
            "  A refusal is not a crash — the engine runs without the plugin. "
            "Fix the cause or uninstall the distribution."
        )
    return 0


def _find(name: str) -> Any:
    from robothor.plugins.inventory import inventory

    for row in inventory():
        if row.name == name:
            return row
    return None


def cmd_plugin_info(name: str) -> int:
    """Everything known about one distribution: manifest, lock row, load state."""
    from robothor.plugins.lockfile import read_lockfile

    row = _find(name)
    if row is None:
        print(
            f"genus plugin info: {name!r} is not an installed plugin distribution. "
            "`genus plugin list` shows what is.",
            file=sys.stderr,
        )
        return 2

    print(f"{row.name} {row.version or '(no version)'}")
    print(f"  state         {row.state}")
    if row.failure_reason:
        print(f"  not loaded    {row.failure_reason}")
    print(f"  groups        {', '.join(row.groups) or '(none)'}")
    if row.contributions:
        for kind, count in row.contributions.items():
            print(f"  {kind:<13} {count}")
    if row.manifest is None:
        print("  manifest      none shipped")
    else:
        print(f"  manifest      contract_version={row.manifest['contract_version']}")
        for kind, names in (row.manifest.get("declared") or {}).items():
            print(f"      declares {kind}: {', '.join(names)}")

    lock_row = read_lockfile().row(name)
    if lock_row is None:
        print("  lock row      none — run `genus plugin sync` to record it")
    else:
        print(
            f"  lock row      enabled={lock_row.enabled} verdict={lock_row.verdict} "
            f"recorded_at={lock_row.recorded_at or '-'}"
        )
        if lock_row.source is None:
            # Absence is information: `genus plugin remove` refuses a row with
            # no source, so an operator wondering why needs to see it here.
            print("  installed by  not this platform (no source recorded)")
        else:
            where = lock_row.source.index_url or lock_row.source.origin
            print(f"  installed by  genus plugin install — {where}")
            if lock_row.source.publisher_key_id:
                print(f"      signed by {lock_row.source.publisher_key_id}")
            if lock_row.source.installed_at:
                print(f"      installed {lock_row.source.installed_at}")
        if lock_row.dist_sha256:
            print(f"  artifact      sha256 {lock_row.dist_sha256}")
        if row.drifted:
            print("  drift         the manifest has changed since it was recorded")
    return 0


def _set_enabled(name: str, enabled: bool) -> int:
    from robothor.plugins.lockfile import set_enabled

    verb = "enable" if enabled else "disable"
    try:
        updated = set_enabled(name, enabled)
    except OSError as exc:
        # A path that is a directory, a read-only filesystem, a full disk. A
        # traceback out of `genus plugin disable` helps nobody.
        print(
            f"genus plugin {verb}: could not write the lockfile for {name!r} "
            f"({type(exc).__name__}: {exc.strerror or exc}).",
            file=sys.stderr,
        )
        return 2
    if updated is None:
        print(
            f"genus plugin {verb}: no lockfile row for {name!r}. Run "
            "`genus plugin sync` to record the installed distributions, then "
            "check the name with `genus plugin list`.",
            file=sys.stderr,
        )
        return 2
    print(f"{name}: {'enabled' if enabled else 'disabled'} — {_RELOAD_HINT}")
    return 0


def cmd_plugin_sync(force: bool = False) -> int:
    """Record every installed plugin distribution in the lockfile."""
    from robothor.plugins.lockfile import sync

    try:
        result = sync(force=force)
    except OSError as exc:
        # Distinct from a refusal: --force cannot fix a read-only filesystem,
        # so the message must not offer it.
        print(
            "genus plugin sync: could not write the lockfile "
            f"({type(exc).__name__}: {exc.strerror or exc}).",
            file=sys.stderr,
        )
        return 2
    if result.path is None:
        print(
            "genus plugin sync: no workspace resolves, so there is nowhere to "
            "write the lockfile. Set ROBOTHOR_WORKSPACE or ROBOTHOR_PLUGIN_LOCKFILE "
            "to an absolute path.",
            file=sys.stderr,
        )
        return 2
    if not result.ok:
        # Exit non-zero: a sync that wrote nothing must not look like one that
        # succeeded, least of all in a script.
        print(f"genus plugin sync: {result.refused}", file=sys.stderr)
        return 2

    # What a forced rebuild cost, before the list of what it wrote — an
    # operator who has just discarded their own decisions should read that
    # first, and `added` where they expected `unchanged` is not a signal.
    if result.discarded_rows or result.discarded_disables or result.rejected_copy:
        print(f"--force discarded {result.discarded_rows} unreadable row(s).")
        if result.discarded_disables:
            print(
                "  these were recorded DISABLED and are now enabled again: "
                + ", ".join(result.discarded_disables)
            )
        if result.rejected_copy:
            print(f"  the unreadable file was kept as {result.rejected_copy}")
        else:
            print("  the unreadable file could not be preserved")
        print()

    print(f"recorded {len(result.recorded)} plugin distribution(s)")
    for row in result.recorded:
        mark = (
            "added"
            if row.name in result.added
            else ("updated" if row.name in result.updated else "unchanged")
        )
        flag = "enabled" if row.enabled else "disabled"
        print(f"  {row.name} {row.version or '-'}  {mark}  {flag}  verdict={row.verdict}")
    for gone in result.removed:
        print(f"  {gone}  dropped — no longer installed")
    if result.added or result.updated or result.removed:
        print(f"\n{_RELOAD_HINT}")
    return 0


def _print_plan(plan: Any, *, would: bool) -> None:
    """One install plan, as the operator reads it.

    The pip command is printed only at the CLI. It carries a temp directory and
    the interpreter's path, which is why the admin route's copy of this object
    leaves it out -- but an operator checking a ``--dry-run`` on their own box
    needs to see the command that would run, and the path is theirs.
    """
    print(f"{plan.name} {plan.version}" + (f" — {plan.summary}" if plan.summary else ""))
    print(f"  source       {plan.origin}" + (f" {plan.index_url}" if plan.index_url else ""))
    if plan.publisher_key_id:
        print(f"  signed by    {plan.publisher_key_id}")
    print(f"  artifact     {plan.filename} ({plan.size} bytes)")
    print(f"  sha256       {plan.sha256}")
    if plan.groups:
        print(f"  contributes  {', '.join(plan.groups)}")
    print(f"  verdict      {plan.verdict} (prompt scan: {plan.prompt_scan})")
    for reason in plan.reasons:
        print(f"      - {reason}")
    if would:
        print("\n  would run:")
        print("    " + " ".join(plan.pip_command))


def cmd_plugin_install(
    spec: str,
    *,
    index: str | None = None,
    sha256: str | None = None,
    accept_review: bool = False,
    scan_prompts: bool = False,
    dry_run: bool = False,
) -> int:
    """Install one plugin, or refuse with the reason on stderr."""
    from robothor.plugins.installer import InstallError, install
    from robothor.plugins.registry import RegistryError

    if not spec:
        print("genus plugin install: name a plugin or a wheel.", file=sys.stderr)
        return 2
    try:
        outcome = install(
            spec,
            index=index,
            sha256=sha256,
            accept_review=accept_review,
            scan_prompts=scan_prompts,
            dry_run=dry_run,
        )
    except (InstallError, RegistryError) as exc:
        # A refusal is the product here. A traceback out of an install would
        # tell the operator nothing they can act on.
        print(f"genus plugin install: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"genus plugin install: could not write the lockfile "
            f"({type(exc).__name__}: {exc.strerror or exc}).",
            file=sys.stderr,
        )
        return 2

    _print_plan(outcome.plan, would=outcome.dry_run)
    if outcome.dry_run:
        print("\nnothing was downloaded into place and nothing was recorded (--dry-run)")
        return 0
    if outcome.note:
        print(f"\nnote: {outcome.note}")
    print(f"\ninstalled {outcome.plan.name} {outcome.plan.version} — {_RELOAD_HINT}")
    return 0


def cmd_plugin_remove(name: str, *, force: bool = False) -> int:
    """Uninstall one plugin this platform installed."""
    from robothor.plugins.installer import InstallError, remove

    if not name:
        print("genus plugin remove: name a distribution.", file=sys.stderr)
        return 2
    try:
        result = remove(name, force=force)
    except InstallError as exc:
        print(f"genus plugin remove: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"genus plugin remove: could not write the lockfile "
            f"({type(exc).__name__}: {exc.strerror or exc}).",
            file=sys.stderr,
        )
        return 2
    if result.get("note"):
        print(f"note: {result['note']}")
    if not result.get("row_dropped"):
        print(f"{name}: no lockfile row to drop")
    print(f"removed {name} — {_RELOAD_HINT}")
    return 0


def cmd_plugin_doctor(as_json: bool = False) -> int:
    """The ``plugins`` category of ``genus doctor``, under its own verb."""
    from robothor.doctor.context import DoctorContext
    from robothor.doctor.render import render_json, render_text
    from robothor.doctor.runner import run_sync

    report = run_sync(DoctorContext(), category="plugins")
    print(render_json(report) if as_json else render_text(report))
    # The same exit-code contract `genus doctor` uses, from the same property:
    # 0 healthy, 1 a required check failed, 2 the doctor itself could not run.
    return int(report.exit_code)


def cmd_plugin(args: argparse.Namespace) -> int:
    """Dispatch one ``genus plugin`` verb. No subcommand means ``list``."""
    command = getattr(args, "plugin_command", None) or "list"
    if command == "list":
        return cmd_plugin_list()
    if command == "sync":
        return cmd_plugin_sync(bool(getattr(args, "force", False)))
    if command == "doctor":
        return cmd_plugin_doctor(bool(getattr(args, "json", False)))
    name = str(getattr(args, "name", "") or "").strip()
    if command == "install":
        return cmd_plugin_install(
            name,
            index=getattr(args, "index", None),
            sha256=getattr(args, "sha256", None),
            accept_review=bool(getattr(args, "accept_review", False)),
            scan_prompts=bool(getattr(args, "scan_prompts", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    if command == "remove":
        return cmd_plugin_remove(name, force=bool(getattr(args, "force", False)))
    if command == "info":
        return cmd_plugin_info(name)
    if command in ("enable", "disable"):
        return _set_enabled(name, command == "enable")
    return cmd_plugin_list()
