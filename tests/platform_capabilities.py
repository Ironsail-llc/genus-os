"""Skip markers for tests whose subject only exists on Linux or with GNU tools.

Production is Linux: the engine runs in a Linux container, and the host
scripts under ``scripts/`` run as systemd units on a Linux box. Some tests
exercise exactly that platform -- a kernel option, ``/proc``, a util-linux or
coreutils binary, GNU option spellings, bash 4.4 -- and on a developer Mac they fail before
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


def _succeeds(argv: list[str], expect: str) -> bool:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == expect


@cache
def _gnu_coreutils() -> bool:
    """GNU spellings the host scripts use: ``date -d``, ``stat -c``, ``touch -d``."""
    return _succeeds(["date", "-d", "@0", "+%s"], "0") and _succeeds(["stat", "-c", "%n", "/"], "/")


@cache
def _bash44() -> bool:
    """Associative arrays, and an empty array expanding cleanly under ``set -u``
    (bash 4.4; before it, ``"${empty[@]}"`` is an unbound-variable error)."""
    return _succeeds(
        ["bash", "-c", 'set -u; declare -A probe=([k]=v); empty=(); echo "${probe[k]}${empty[@]}"'],
        "v",
    )


#: GNU coreutils (``date -d``, ``stat -c``, ``touch -d``, ``mv -T``); BSD and
#: macOS spell these differently or not at all.
needs_gnu_coreutils = pytest.mark.skipif(
    not _gnu_coreutils(),
    reason="needs GNU coreutils (date -d, stat -c, touch -d, mv -T); these are BSD",
)

#: bash 4.4+ (associative arrays, ``mapfile``, empty arrays under ``set -u``);
#: macOS ships 3.2.
needs_bash44 = pytest.mark.skipif(
    not _bash44(), reason="needs bash 4.4 or later; this bash is older (macOS ships 3.2)"
)

#: Linux file-system semantics (setgid directories, group inheritance).
needs_linux = pytest.mark.skipif(
    sys.platform != "linux", reason="needs Linux file-system semantics (setgid directories)"
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

#: The abstract Unix-socket namespace (``\0name``); Linux only.
needs_abstract_sockets = pytest.mark.skipif(
    sys.platform != "linux", reason="needs the Linux abstract Unix-socket namespace"
)
