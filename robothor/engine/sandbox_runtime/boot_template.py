"""What actually runs inside the sandbox, before the agent's snippet does.

A template rather than a module, because it is parameterised (which packages
the import guard refuses, which `prctl` number to set) and because it must run
under an isolated interpreter that cannot import anything of ours. It lives
HERE, beside ``genus_tools.py``, because the two files copied into a sandbox
are one subject — and because ninety lines of in-sandbox source inside the
handler is the shape the client module was deliberately not written in.

It does three things the engine cannot do from outside:

* puts the per-call directory on ``sys.path`` (``-I`` deliberately leaves it
  off) so ``genus_tools`` is importable and nothing else the venv holds is on
  the path by accident;
* refuses the engine's own packages at import time. Defence in depth — a
  snippet can filter ``sys.meta_path`` in one line — and not the boundary,
  which is that the child's environment holds nothing worth importing the
  engine for;
* kills what the snippet started, on the way out, FROM INSIDE. The engine also
  kills (the process group, plus the descendants its census saw) but the engine
  can only sample: a snippet that spawns fifteen detached children and exits
  does both inside one sampling tick, and measured, 15 of 15 survived. Here the
  parent chain is intact and there is no race. A snippet that calls
  ``os._exit`` skips this, which is what the engine-side kill is for.
"""

from __future__ import annotations

__all__ = ["BOOT_TEMPLATE"]

BOOT_TEMPLATE = '''\
"""Written by the engine. Runs the agent's snippet with genus_tools importable."""

import ctypes
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

# PR_SET_CHILD_SUBREAPER. A double-forked grandchild normally reparents to
# init, which takes it off the chain the engine walks when it is time to kill
# everything this snippet started. With this set, it reparents to the snippet
# instead and stays findable. The snippet can undo it, like it can undo the
# import guard; both are there to make the honest case complete rather than to
# stop a hostile one.
try:
    ctypes.CDLL(None, use_errno=True).prctl({subreaper}, 1, 0, 0, 0)
except Exception:
    pass

_GUARDED = {guarded!r}


class _RefuseEngineImports:
    """The engine's own packages are not part of the sandbox's vocabulary."""

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy hook
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _GUARDED:
            raise ImportError(
                fullname
                + " is not importable from execute_code. Call tools through "
                "genus_tools instead."
            )
        return None


sys.meta_path.insert(0, _RefuseEngineImports())


def _descendants():
    """Everything below this process, deepest last. Complete, not a sample:
    here the parent chain cannot have been broken yet."""
    seen, frontier = [], [os.getpid()]
    while frontier and len(seen) < 4096:
        nxt = []
        for parent in frontier:
            try:
                tasks = os.listdir("/proc/%d/task" % parent)
            except OSError:
                continue
            for tid in tasks:
                try:
                    with open("/proc/%d/task/%s/children" % (parent, tid)) as fh:
                        kids = [int(x) for x in fh.read().split()]
                except (OSError, ValueError):
                    continue
                for kid in kids:
                    if kid > 1 and kid not in seen:
                        seen.append(kid)
                        nxt.append(kid)
        frontier = nxt
    return seen


def _reap():
    """Kill what this snippet started, from here, where the chain is intact.

    The ENGINE also kills — the process group, plus the descendants its census
    saw — and that is what catches a timeout or a cancellation. But the engine
    can only sample, and a snippet that spawns fifteen detached children and
    exits does both inside one sampling tick: measured, 15 of 15 survived. Here
    there is no race.

    Which is also the limit: a snippet that calls os._exit never reaches this
    `finally`, and a child it started in its own session is outside the group
    the engine kills. Those two together are a deliberate, reproducible escape,
    and nothing on this side can prevent them — the snippet owns its own exit
    path. That is stated in the tool's docs and in its timeout message rather
    than papered over.
    """
    import signal
    for pid in reversed(_descendants()):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


import runpy  # noqa: E402

try:
    runpy.run_path(os.path.join(_DIR, "snippet.py"), run_name="__main__")
finally:
    _reap()
'''
