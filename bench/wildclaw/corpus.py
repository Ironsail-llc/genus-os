"""Where the WildClawBench task specs are, if they are anywhere.

The benchmark is a separate checkout, not part of this repository, and its
location is an INSTANCE fact — a test that hardcodes one operator's home
directory is the platform/instance leak the root `CLAUDE.md` names as the
number-one cause of data leaks, and the leak gate refuses it.

So: an explicit environment variable, or the conventional location under the
running user's home, or nothing. Tests that read the corpus skip when it
returns None, which keeps them honest on a machine without the checkout and
live on one with it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Point this at a WildClawBench checkout's `tasks/` directory to run the
#: corpus-backed tests anywhere.
TASKS_DIR_ENV = "WILDCLAW_TASKS_DIR"

#: Where `bench/wildclaw/README.md` tells an operator to clone it.
_CONVENTIONAL = ("robothor-bench", "WildClawBench", "tasks")


def tasks_dir() -> Path | None:
    """The task specs, or None when this machine has no checkout."""
    override = os.environ.get(TASKS_DIR_ENV, "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None
    candidate = Path.home().joinpath(*_CONVENTIONAL)
    return candidate if candidate.is_dir() else None
