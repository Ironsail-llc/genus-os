"""Turning a run into words, for a person or for a program — never both.

Two audiences, two streams. With ``--json`` the document on stdout is the whole
contract and every human line goes to stderr, so a caller can pipe stdout into
``jq`` and still read the progress. Without it, stdout is the terminal.

Nothing here formats a secret, and nothing it is given carries one: the
provider key is resolved at the moment of the probe and never stored in
``ctx.answers``, so it cannot reach a plan row, a step detail or the JSON. The
Telegram token is an answer but is never rendered. The setup token appears
exactly once, inside the URL the link step prints, which is the design — it is
the credential, and it goes to the operator's terminal and nowhere else.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.init.plan import InitResult, PlanEntry, StepOutcome

__all__ = ["MARKS", "render_json", "render_outcome", "render_plan", "render_summary"]

#: One character per plan status, so a seventeen-row plan reads at a glance.
MARKS = {
    "create": "+",
    "exists": "=",
    "skip": "-",
    "warn": "~",
    "blocked": "!",
}


def render_plan(entries: list[PlanEntry], substrate_name: str) -> list[str]:
    """The plan, one line per step, plus a fix hint under anything broken."""
    lines = [f"  Plan ({substrate_name}):"]
    width = max((len(row.id) for row in entries), default=0)
    for row in entries:
        mark = MARKS.get(row.status, "?")
        lines.append(f"    {mark} {row.id.ljust(width)}  {row.detail}")
        if row.fix_hint:
            lines.append(f"      -> {row.fix_hint}")
    lines.append("")
    lines.append("    + create   = already there   - skipped   ~ optional, not usable   ! blocked")
    return lines


def render_outcome(outcome: StepOutcome, width: int = 0) -> str:
    """One applied step."""
    verb = {
        "applied": "",
        "skipped": "skipped: ",
        "planned": "would run: ",
        "failed": "FAILED: ",
    }.get(outcome.status, "")
    return f"    {outcome.id.ljust(width)}  {verb}{outcome.detail}".rstrip()


def render_summary(result: InitResult, *, workspace: str) -> list[str]:
    """The last few lines: what happened, and what to do next."""
    if result.exit_code and result.blocked:
        named = ", ".join(result.blocked)
        return [
            "",
            f"  Nothing was written: {len(result.blocked)} required check(s) failed ({named}).",
            "  Fix the items marked ! above and run `genus init` again.",
        ]
    if result.exit_code:
        failed = [row for row in result.steps if row.status == "failed"]
        detail = failed[0].detail if failed else "a step failed"
        step_id = failed[0].id if failed else "?"
        applied = [row.id for row in result.steps if row.status == "applied"]
        lines = [
            "",
            f"  Stopped at `{step_id}`: {detail}",
        ]
        if applied:
            # Naming what DID land matters more here than anywhere else: the
            # operator has to know the instance is part-built before deciding
            # whether to fix forward or start over.
            lines.append(f"  Applied before it: {', '.join(applied)}.")
        lines.append("  Everything applied was recorded — `genus init` resumes where it stopped.")
        if result.first_run_url:
            lines.append("  The first-run link above still works; use it to finish in the browser.")
        else:
            lines.append("  Need a way in? `genus auth setup-link` mints a fresh /setup link.")
        return lines
    planned = any(row.status == "planned" for row in result.steps)
    if planned:
        return ["", "  Dry run: nothing was written."]
    lines = ["", "  Genus OS is initialized.", f"    Workspace: {workspace}"]
    if result.first_run_url:
        lines.append("    Open the link above to finish setup in the browser.")
    return lines


def render_json(result: InitResult) -> str:
    """The whole run as one document. Stdout carries this and nothing else."""
    return json.dumps(result.as_dict(), indent=2)
