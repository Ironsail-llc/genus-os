"""Which of one model turn's tool calls may run at the same time.

MEASURED 2026-09-16 across ten WildClawBench runs: Genus issued **zero**
parallel tool calls, because ``_run_loop`` executed ``assistant_msg.tool_calls``
one at a time and nothing told the model it could ask for more than one. The
competing harness batched on twenty turns of a single image task and eight of
another, and under a fixed per-task time budget that is the difference between
finishing and spending 1200 s on 150 sequential calls.

The rule this module implements is deliberately narrower than "run whatever
looks independent":

* A call is eligible only when the platform has **classified** it read-only —
  ``robothor.engine.tools.read_only.declared_read_only_tools`` — so an
  unclassified tool, a plugin tool nobody declared, and every write are all
  ineligible by omission rather than by anyone remembering to list them.
* Below that classification sits a hard list. ``exec``, ``execute_code``, and
  anything spelled ``send_*`` or ``spawn_*`` are never eligible, whatever some
  table says about them. The classification is a judgement made elsewhere and
  could be wrong; these four shapes are the ones where being wrong is
  irreversible.
* A tool matching the agent's ``human_approval_tools`` patterns is never
  eligible: the gate for it BLOCKS on an operator, and a batch waiting on a
  person is not a batch.
* **The first ineligible call ends grouping for the whole turn.** It runs
  alone, and so does every call after it, in the model's order. Reordering a
  write relative to anything — including relative to a read that may observe
  what it wrote — is not a performance decision this layer is allowed to make.

What this module does NOT do is execute anything. It returns index groups, and
the caller admits and runs each group; keeping the policy free of the loop is
what lets the policy be tested against a table of names instead of a run.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "DEFAULT_PARALLEL_TOOL_CALLS",
    "MAX_PARALLEL_TOOL_CALLS",
    "NEVER_PARALLEL_PREFIXES",
    "NEVER_PARALLEL_TOOLS",
    "clamp_parallel_limit",
    "is_parallel_safe",
    "parallel_limit",
    "plan_batches",
]

#: How many admitted read-only calls run at once when the operator has not
#: said otherwise. Four is the same number ``analyze_image`` fans out at and
#: for the same reason: it is comfortably below every provider's per-key
#: concurrency and above the point where the win stops being visible.
DEFAULT_PARALLEL_TOOL_CALLS = 4

#: The ceiling an operator cannot raise past. A turn asking for more in-flight
#: calls than this is asking the instance to rate-limit itself against its own
#: providers — the failure the credential pool recorded on 2026-08-25, where a
#: burst capped one key and stopped the whole fleet.
MAX_PARALLEL_TOOL_CALLS = 16

#: Never concurrent, whatever any classification table says. ``exec`` and
#: ``execute_code`` run arbitrary code, which is every side effect at once;
#: ``browser`` and ``desktop_*`` drive one shared machine.
NEVER_PARALLEL_TOOLS: frozenset[str] = frozenset(
    {
        "exec",
        "execute_code",
        "browser",
        "ask_user",
    }
)

#: And never concurrent by SHAPE. ``send_*`` reaches a person, ``spawn_*``
#: starts a run, ``desktop_*`` moves a real pointer on a real screen.
NEVER_PARALLEL_PREFIXES: tuple[str, ...] = ("send_", "spawn_", "desktop_")


def clamp_parallel_limit(requested: int) -> int:
    """How many calls may be in flight, within what the platform allows.

    Non-positive means one — sequential, the pre-2026-09 behaviour — rather
    than "unbounded". An operator setting 0 to turn the feature off must get
    the feature off, not an unbounded fan-out; that inversion is the defect
    ``vision_batch._max_total_chars`` was corrected for.
    """
    if requested <= 0:
        return 1
    return min(requested, MAX_PARALLEL_TOOL_CALLS)


def parallel_limit() -> int:
    """The configured ceiling, clamped. Never raises."""
    try:
        from robothor.settings import get_settings

        configured = int(get_settings().engine.parallel_tool_calls)
    except Exception:  # noqa: BLE001 - a missing config is the default, not a crash
        configured = DEFAULT_PARALLEL_TOOL_CALLS
    return clamp_parallel_limit(configured)


def is_parallel_safe(
    tool_name: str,
    *,
    read_only: Iterable[str],
    human_approval: Iterable[str] = (),
) -> bool:
    """Whether this one call may share a batch with another.

    Args:
        tool_name: the registered name the model asked for.
        read_only: what this instance has classified read-only.
        human_approval: the agent's ``human_approval_tools`` glob patterns.
    """
    if tool_name in NEVER_PARALLEL_TOOLS:
        return False
    if tool_name.startswith(NEVER_PARALLEL_PREFIXES):
        return False
    if any(fnmatch.fnmatch(tool_name, pattern) for pattern in human_approval):
        return False
    return tool_name in frozenset(read_only)


def plan_batches(
    tool_names: Sequence[str],
    *,
    read_only: Iterable[str],
    human_approval: Iterable[str] = (),
    limit: int = DEFAULT_PARALLEL_TOOL_CALLS,
) -> list[list[int]]:
    """Group a turn's calls into execution batches, by INDEX.

    Indices rather than names, so the caller can keep the model's own order and
    ``tool_call_id``s: the result of batch *k* position *j* is the result of
    ``tool_names[plan[k][j]]`` and nothing downstream has to match on a name
    that may appear twice in one turn.

    The returned groups, concatenated, are exactly ``range(len(tool_names))``
    in order — which is what makes "results return in the model's order" a
    property of the plan rather than of the loop reading it.
    """
    bound = clamp_parallel_limit(limit)
    safe_names = frozenset(read_only)
    patterns = tuple(human_approval)

    plan: list[list[int]] = []
    current: list[int] = []
    serialised = False  # an ineligible call has been seen; nothing groups after it

    for index, name in enumerate(tool_names):
        eligible = not serialised and is_parallel_safe(
            name, read_only=safe_names, human_approval=patterns
        )
        if not eligible:
            if current:
                plan.append(current)
                current = []
            plan.append([index])
            serialised = True
            continue
        current.append(index)
        if len(current) >= bound:
            plan.append(current)
            current = []

    if current:
        plan.append(current)
    return plan
