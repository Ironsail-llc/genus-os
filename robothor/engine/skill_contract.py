"""A contract the agent was handed by a SKILL is still a contract.

MEASURED 2026-09-16, probed read-only against two real benchmark tasks:

    task_2 prompt-only    required_deliverables -> []
    task_2 prompt+skill   required_deliverables -> ['<results path>']
    task_5 prompt-only    required_deliverables -> ['<results path>']

``task_text_for_run`` reads the PROMPT — the persisted ``run.task_text``, the
live session's originating message, or the CRM task row. None of those contains
the body of a skill the agent loaded mid-run. So for a task whose output path is
stated only inside its skill, every consumer of the contract (the mid-run
check-in comparison, the re-ask, the finalizer's verdict) was reading a task
description that never mentioned the file it would be graded on, and the contract
was empty for the run that needed it most.

The fix is one seam, not three: the bodies a run actually loaded are remembered
on the session and appended to the same accessor all three consumers share, so
they cannot disagree about what the task asked for — the invariant the resumed-run
defect (hostile review I5) was corrected to preserve.

Bounded on purpose. A skill body can be tens of thousands of characters and this
text is scanned by the contract extractor on every check-in; keeping the whole
library in memory to answer "which path did it name" would cost more than the
answer is worth.
"""

from __future__ import annotations

import contextlib
from typing import Any

__all__ = [
    "SKILL_TEXT_ATTR",
    "SKILL_TEXT_MAX_CHARS",
    "loaded_skill_text",
    "remember_skill_text",
]

#: Where the loaded bodies hang off the session.
SKILL_TEXT_ATTR = "_loaded_skill_text"

#: How much skill text one run keeps. Generous enough for the handful of
#: bodies a run loads, far below ``TASK_TEXT_MAX_CHARS`` so appending it can
#: never be what pushes the task text over its own column limit.
SKILL_TEXT_MAX_CHARS = 8_192

#: The tools that hand an agent a skill body. ``list_skills`` is deliberately
#: absent: it returns catalogue metadata, and a description an agent merely saw
#: in a list is not an instruction it was given.
SKILL_TOOLS = frozenset({"skill_view", "invoke_skill"})


def remember_skill_text(session: Any, tool_name: str, tool_output: Any) -> None:
    """Keep the body of a skill this run just loaded. Never raises."""
    if tool_name not in SKILL_TOOLS or not isinstance(tool_output, dict):
        return
    body = tool_output.get("content")
    if not isinstance(body, str) or not body.strip():
        return
    with contextlib.suppress(AttributeError, TypeError):
        kept = str(getattr(session, SKILL_TEXT_ATTR, "") or "")
        if body in kept or len(kept) >= SKILL_TEXT_MAX_CHARS:
            return
        joined = f"{kept}\n\n{body}" if kept else body
        setattr(session, SKILL_TEXT_ATTR, joined[:SKILL_TEXT_MAX_CHARS])


def loaded_skill_text(session: Any) -> str:
    """Every skill body this run was handed, or "" when it was handed none."""
    if session is None:
        return ""
    return str(getattr(session, SKILL_TEXT_ATTR, "") or "")
