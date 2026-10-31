"""Skip markers for tests whose subject only exists on Linux or with GNU tools.

Production is Linux: the engine runs in a Linux container, and the host
scripts under ``scripts/`` run as systemd units on a Linux box. Some tests
exercise exactly that platform -- a kernel option, ``/proc``, a util-linux or
coreutils binary, GNU ``date -d`` -- and on a developer Mac they fail before
they test anything. Each marker here asks for the CAPABILITY, not the OS name,
so a Mac with GNU coreutils on PATH still runs what it can, and each reason
names what is missing so a skip is never mistaken for a pass.

Only for behaviour that is genuinely platform-bound. A script that can be
written portably is fixed instead (``tests/test_suite_hygiene.py`` holds the
portability gates).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest


def needs_tools(*names: str) -> pytest.MarkDecorator:
    """Skip unless every named binary resolves on PATH."""
    missing = [name for name in names if shutil.which(name) is None]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"needs {', '.join(missing)} on PATH (Linux util-linux/coreutils); not installed here",
    )


@cache
def _gnu_date() -> bool:
    try:
        result = subprocess.run(
            ["date", "-d", "@0", "+%s"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "0"


@cache
def _bash4() -> bool:
    try:
        result = subprocess.run(
            ["bash", "-c", "declare -A probe=([k]=v); echo ${probe[k]}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "v"


#: GNU ``date -d <string>``; BSD date spells it ``-j -f`` and rejects ``-d``.
needs_gnu_date = pytest.mark.skipif(
    not _gnu_date(), reason="needs GNU date (date -d); this date is BSD"
)

#: bash 4+ (associative arrays, ``mapfile``); macOS ships bash 3.2.
needs_bash4 = pytest.mark.skipif(
    not _bash4(), reason="needs bash 4 or later; this bash is older (macOS ships 3.2)"
)

#: ``/proc`` -- per-process environ, task lists.
needs_proc = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="needs /proc (Linux); this kernel has none"
)

#: ``prctl(PR_SET_CHILD_SUBREAPER)`` and ``PR_SET_DUMPABLE`` are Linux prctls.
needs_linux_prctl = pytest.mark.skipif(
    sys.platform != "linux", reason="needs Linux prctl (PR_SET_CHILD_SUBREAPER, PR_SET_DUMPABLE)"
)

#: ``SO_PEERCRED`` identifies a Unix-socket peer; Linux only.
needs_peer_credentials = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"),
    reason="needs SO_PEERCRED (Linux) to identify the peer; this kernel refuses every peer",
)
