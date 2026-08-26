"""The files a task asked for, and whether they are there yet.

Measured repeatedly on WildClawBench: a task says "save the result to
/tmp_workspace/results/result.png", the grader awards unconditional credit
for that file merely existing, and the agent writes seven images into
`results/` under other names. The string `result.png` appeared exactly once
in one whole transcript — in the prompt. In another run the agent printed
the complete answer to stdout and spent its final calls re-deriving it
rather than writing the two lines the task asked for.

The engine could not notice. `verify_output` judges the agent's narration,
never the workspace, and the deadline warning says "write your partial
answer" without knowing where.

This is not benchmark-specific. Production tasks name deliverables
constantly — write the report to X, save the export to Y — and an agent that
reports success with no file there is the completion-contract failure this
platform already exists to catch.

Deliberately conservative. A false positive nags an agent about a file it
was never asked to produce, and a control that nags gets ignored, which is
how a real warning goes unread.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Absolute paths that look like a FILE — a suffix is required, because a
#: bare directory ("work inside /tmp_workspace") is a location, not a
#: deliverable. Extensions are capped at 5 characters so a sentence ending
#: mid-path does not produce a phantom file.
#:
#: Tokens are runs of ASCII path characters, and each is matched once against
#: an anchored pattern. Linear in the input, and language-agnostic: CJK text
#: contains no spaces, so splitting on whitespace found NOTHING in the
#: Chinese task prompts this exists for — caught by probing a real prompt,
#: after the unit tests had all passed.
#:
#: Three regex attempts preceded this, each flagged by CodeQL
#: (py/polynomial-redos) and each correctly. Nested quantifiers backtracked
#: on `/-/-/-/...`; flattening them left `.` competing with the literal `\.`
#: and backtracked on `/a.a.a...`; removing dots from the body still left an
#: unanchored scan, which is quadratic on `////...` because every start
#: position rescans to the end. This pattern runs over untrusted task text,
#: so a crafted prompt must not be able to hang the engine before the agent
#: takes a step — and the fix for that is not a cleverer regex, it is not
#: scanning free text with one.
#:
#: A suffix is required, because a bare directory ("work inside
#: /tmp_workspace") is a location, not a deliverable. Extensions are capped
#: at 5 characters so a sentence ending mid-path yields no phantom file.
#: Deliberately ASCII, not `\w`: Python's `\w` matches CJK, which would glue
#: a path to the sentence around it.
_PATH_CHARS_RE = re.compile(r"[A-Za-z0-9_\-/.]+")
_PATH_TOKEN_RE = re.compile(r"\A(/[A-Za-z0-9_\-/]+\.[A-Za-z0-9]{1,5})\Z")

#: Punctuation a path picks up from prose: backticks, quotes, a trailing
#: comma or full stop, parentheses.
#: A trailing dot is prose punctuation, never part of a filename — and it
#: must be in this set or `strip` halts on it before reaching the backtick
#: underneath. Stripping it cannot damage a real path: a file ending in `.`
#: has no extension and would not match anyway.
_TRIM = "`'\".,;:!?()[]<>" + "\u3002\uff0c\u201c\u201d"


#: Directory names that hold a task's INPUTS. A path under one of these is
#: something the agent reads, never something it must produce, and warning
#: about a missing input would send it looking for the wrong problem.
_INPUT_DIRS = ("/input/", "/inputs/", "/gt/", "/fixtures/", "/data/", "/exec/")

#: Most it will report. A prompt naming dozens of files is describing a tree,
#: not a deliverable set, and a wall of paths is noise.
_MAX_PATHS = 10


def declared_paths(text: str | None) -> list[str]:
    """Absolute file paths the task text asks the agent to produce.

    Order-preserving and de-duplicated, so the note reads in the order the
    task stated them.
    """
    if not text:
        return []
    found: list[str] = []
    for raw in _PATH_CHARS_RE.findall(str(text)):
        token = raw.strip(_TRIM)
        if not token.startswith("/"):
            continue
        match = _PATH_TOKEN_RE.match(token)
        if not match:
            continue
        path = match.group(1)
        if any(part in path for part in _INPUT_DIRS):
            continue
        if path not in found:
            found.append(path)
        if len(found) >= _MAX_PATHS:
            break
    return found


def missing_deliverables_note(
    paths: list[str], remaining: int, workspace: str | Path | None = None
) -> str | None:
    """A note naming the declared files that are not on disk yet, or None.

    Returns None when nothing was declared or everything already exists — the
    silent case has to be the common one, or the note stops being read.

    Every path is confined to `workspace` before it is touched. Task text is
    untrusted input in any deployment where someone else can file a task, and
    these paths reach the filesystem; without a workspace to confine to,
    nothing is checked at all. Confinement is also the more correct rule — a
    file outside the agent's workspace is not the deliverable it is graded on.
    """
    if not paths or not workspace:
        return None
    try:
        root = Path(workspace).resolve()
    except (OSError, ValueError):
        return None
    missing: list[str] = []
    for path in paths:
        try:
            candidate = Path(path).resolve()
            if not candidate.is_relative_to(root):
                continue
            if not candidate.exists():
                missing.append(path)
        except (OSError, ValueError):
            # An unparseable path is not evidence of anything; skip it rather
            # than reporting a file the task never really named.
            continue
    if not missing:
        return None
    listed = ", ".join(missing)
    return (
        f"[SYSTEM] About {int(remaining)}s left, and the task asked for files "
        f"that do not exist yet: {listed}. Write them NOW with whatever you "
        "have — an incomplete file at the requested path is worth more than a "
        "perfect answer that was never saved, and partial results are graded "
        "per criterion. Say plainly what is missing from them."
    )


def deadline_note(
    elapsed: float,
    hard_timeout: float,
    task_text: str | None,
    workspace: str | Path | None = None,
) -> str | None:
    """The full wrap-up note: the time warning, plus any missing deliverable.

    Composed here rather than in the runner because it is one question — the
    run is nearly out of time, so what should the agent do about it — and the
    answer is much more useful when it can name the file. "Write your partial
    answer" without a path is how an agent ends up with seven images in
    `results/` and none of them called what the task asked for.
    """
    from robothor.engine.run_budget import deadline_warning

    base = deadline_warning(elapsed, hard_timeout)
    if not base:
        return None
    missing = missing_deliverables_note(
        declared_paths(task_text),
        remaining=max(0, int(hard_timeout - elapsed)),
        workspace=workspace,
    )
    return f"{base}\n{missing}" if missing else base
