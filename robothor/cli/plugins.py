"""`robothor plugin list` — what is installed, and what the engine will do with it.

The seam shipped with ten entry-point groups and no operator surface at all:
nothing told you what was installed, what it contributed, or why something was
refused. That was tolerable while the answer was always "nothing"; it is not
now that plugins actually load.

It matters most for the manifest ladder. `ROBOTHOR_PLUGIN_MANIFEST_MODE`
defaults to `observe` because requiring a `genus-plugin.yaml` is a breaking
change for anything published before it existed — and an operator deciding
whether to promote to `enforce` needs to know which installed distributions
would stop loading. That question had no answer until this command.
"""

from __future__ import annotations


def cmd_plugin_list() -> int:
    """Print installed plugins, what they contribute, and any refusals."""
    from robothor.plugins.loader import load_plugins
    from robothor.plugins.manifest import MANIFEST_NAME, manifest_mode

    mode = manifest_mode()
    result = load_plugins()

    print(f"manifest mode: {mode}")
    if mode != "enforce":
        print(
            f"  NOTE: {mode} still IMPORTS a distribution that ships no "
            f"{MANIFEST_NAME}. The refuse-before-import guarantee applies only "
            "in enforce."
        )
    print()

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

    if contributions:
        print("Loaded:")
        for kind, names in contributions.items():
            print(f"  {kind:<11} {', '.join(names)}")
    else:
        print("Loaded: nothing (every entry point was refused — see below)")

    if result.failures:
        print()
        print("Refused:")
        for f in result.failures:
            print(f"  {f.name} [{f.group}]: {f.reason}")
        print()
        print(
            "  A refusal is not a crash — the engine runs without the plugin. "
            "Fix the cause or uninstall the distribution."
        )
    return 0
