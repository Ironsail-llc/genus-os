"""Every long-running Genus process, not only the engine.

Review R5. Round 1 called ``harden_process()`` from ``daemon.main`` and nowhere
else, so on this box the bridge (which holds the SSO secret and the database
credentials), the orchestrator, the vision service, the four connector bridges
and the two MCP servers all stayed dumpable — and an ``exec`` child is the same
uid as every one of them. Hardening one process out of ten is not a boundary,
it is a statistic.

Also R5's second half: the denylist matched ``/proc/<pid>/environ`` literally,
so ``/proc/<pid>//environ`` walked straight past it. A path is normalised
before it is matched now, which is the only way a path denylist can mean
anything.

The entry-point test imports each module and asserts the call is THERE, rather
than starting ten services. A grep would pass on a commented-out line; an
import-and-inspect will not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from robothor.engine.secret_paths import exec_reads_secret

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every process Genus starts and leaves running under the operator's uid.
#: Named as module paths rather than unit names because the test imports them:
#: a unit file can be renamed without the code changing, and it is the code
#: that has to make the call.
ENTRY_POINTS = [
    "robothor/engine/daemon.py",
    "crm/bridge/bridge_service.py",
    "robothor/vision/service.py",
    "robothor/connectors/rest_mcp_bridge.py",
    "robothor/api/mcp.py",
]


def _calls_harden(path: Path) -> bool:
    """Whether this module contains a real call to ``harden_process``.

    Parsed, not grepped: a grep matches the import, a comment and a docstring
    mentioning it, none of which hardens anything.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else ""
            )
            if name in {"harden_process", "_harden_and_state_posture"}:
                return True
    return False


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_every_entry_point_hardens_itself(module):
    path = REPO_ROOT / module
    if not path.is_file():
        pytest.skip(f"{module} is not present in this checkout")
    assert _calls_harden(path), (
        f"{module} starts a long-running process under the operator's uid and never "
        "calls harden_process(), so an agent's exec child can read its environment "
        "out of /proc"
    )


def test_the_helper_is_importable_from_one_place():
    """One helper, so a new entry point copies a single line and cannot get a
    subtly different version of it."""
    from robothor.engine.process_hardening import harden_process

    assert callable(harden_process)


# ── the // gap ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/self/environ",
        "cat /proc/self//environ",
        "cat /proc//self/environ",
        "cat /proc/self/./environ",
        "cat /proc/1234/task/1234/environ",
        "cat /proc/self/task/99/environ",
        "grep -a FAKE /proc/$PPID//environ",
        "tr '\\0' '\\n' < /proc/self//environ",
        "cat /proc/self//cmdline",
        "cat //proc/self/environ",
    ],
)
def test_every_spelling_of_the_procfs_read_is_refused(command):
    """``/proc/<pid>//environ`` slipped the literal match. A path denylist that
    matches the string rather than the path is a denylist that means nothing —
    the kernel resolves all of these to the same file."""
    assert exec_reads_secret(command) is not None, f"{command!r} was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/loadavg",
        "cat /proc/self/status",
        "ls /proc/self",
        "cat /proc//loadavg",
        "echo /environ",
        "cat environ.txt",
    ],
)
def test_ordinary_proc_reads_still_work(command):
    """A denylist that eats `/proc/self/status` breaks every health script, and
    a control that breaks scripts gets turned off."""
    assert exec_reads_secret(command) is None, f"{command!r} was refused"
