"""Skills: what the platform ships, and what this instance reads instead.

Two trees answer the same names. ``agents/skills/`` is the library the
platform ships; the instance's own directory (``brain/skills`` unless
``ROBOTHOR_INSTANCE_SKILLS_DIR`` says otherwise) holds everything its agents
wrote. The instance wins a collision, which is the point -- an instance must
be able to correct a procedure it was given -- but it is invisible on disk:
the bundled file is untouched, so a checkout looks clean while every agent
reads something else.

These two checks are the reading an operator cannot get any other way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, info, ok

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext


async def _shadowed(ctx: DoctorContext) -> Result:
    """Which bundled skills this instance has replaced with its own.

    Not a failure: replacing a shipped procedure with a better one is a
    supported thing for an instance to do, and the original is one ``mv``
    away. It is reported because nothing else reports it -- the bundled file
    is still on disk and still tracked, so the only symptom of a shadow is an
    agent following a procedure that is not the one you are reading.

    If a name here surprises you, ``skill_view`` names the layer each body
    came from, and moving the instance's copy out of the way (``skill_archive``
    on it) makes the platform's live again.
    """

    def _read() -> tuple[tuple[str, ...], str]:
        from robothor.engine.skills import instance_skills_dir, shadowed_skill_names

        return shadowed_skill_names(), str(instance_skills_dir())

    try:
        names, where = await ctx.run_blocking(_read)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        return fail(f"the skill directories could not be read ({type(exc).__name__})")

    if not names:
        return ok("no bundled skill is being shadowed by this instance")
    return info(
        f"{len(names)} bundled skill(s) are shadowed by this instance's own copies in "
        f"{where}: {', '.join(names)}. Agents read the instance's version; the bundled "
        f"files are unchanged on disk."
    )


async def _instance_dir(ctx: DoctorContext) -> Result:
    """Where agent-written skills land, and whether that is somewhere safe.

    Two ways to get this wrong, and both are quiet:

    * **Inside the platform tree.** Every skill an agent writes reappears as an
      untracked directory under ``agents/skills/`` -- one ``add -A`` from being
      committed into the platform repository, and deleted by a clean checkout.
      That is the defect this boundary exists to close, so it fails.
    * **Outside the workspace.** Snapshots capture workspace paths, so a
      directory elsewhere on the filesystem is skills nobody is backing up.

    The fix is the same for both: point ``ROBOTHOR_INSTANCE_SKILLS_DIR`` at a
    directory inside the workspace and outside ``agents/``, or unset it and
    take the default.
    """

    def _read() -> tuple[str, str, str]:
        from robothor.engine.skills import bundled_skills_dir, instance_skills_dir
        from robothor.settings.sources import workspace_path

        resolved = workspace_path()
        return (
            str(instance_skills_dir()),
            str(bundled_skills_dir()),
            str(resolved) if resolved else "",
        )

    try:
        instance_dir, bundled_dir, workspace = await ctx.run_blocking(_read)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        return fail(f"the instance skills directory could not be resolved ({type(exc).__name__})")

    from pathlib import Path

    instance = Path(instance_dir)
    bundled = Path(bundled_dir)

    if instance == bundled or bundled in instance.parents:
        return fail(
            f"the instance skills directory is inside the platform's own "
            f"({instance_dir} is under agents/skills). Every skill an agent writes "
            f"would land in the tracked tree again: set ROBOTHOR_INSTANCE_SKILLS_DIR "
            f"to a directory outside it, or unset it for the default."
        )

    if workspace:
        root = Path(workspace)
        if root not in instance.parents:
            return fail(
                f"the instance skills directory is outside the workspace ({instance_dir}). "
                f"A snapshot captures workspace paths, so the skills this instance learns "
                f"would not be in any backup. Point ROBOTHOR_INSTANCE_SKILLS_DIR inside "
                f"{workspace}, or unset it for the default."
            )

    return ok(f"agent-written skills land in {instance_dir}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="skills.instance_dir",
        title="Agent-written skills land outside the platform tree",
        category="skills",
        severity="required",
        run=_instance_dir,
    ),
    Check(
        id="skills.shadowed",
        title="Which bundled skills this instance has replaced",
        category="skills",
        severity="info",
        run=_shadowed,
    ),
)
