"""What a snippet's child processes did, folded into what the snippet did.

The engine-side half of ``sandbox_runtime/spawn_recorder.py``. The recorder
writes one entry per spawn; this module answers two questions about them and
nothing else:

* which of them were HTTP the snippet made through a CLI, and therefore belong
  in ``http_calls`` beside the ``urllib`` ones — so the unread-response rule,
  ``lost_responses``, the observation ledger and "the recorder outranks the
  heuristic" all apply to a ``curl`` write without a second copy of any of
  them (:func:`merge_spawned_http`);
* what the rest were, as a summary the model and an operator can read, and
  whether any of them could reach the network in a way no recorder in that
  process sees through (:func:`spawn_summary`). That last list is the honest
  statement that the run may have changed something nothing witnessed. It is
  a note, not a ledger entry: the ledger stays conservative, and a false "you
  changed something" is the thing that gets a control ignored.

Pure functions over already re-bounded records — the loader in
``code_exec_result`` has checked the file's size and every field's shape
before anything here sees it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from robothor.engine.sandbox_runtime.spawn_recorder import EGRESS_TOOLS, HTTP_CLIS

__all__ = ["MAX_SPAWNED_PROGRAMS", "merge_spawned_http", "spawn_summary"]

#: How many distinct program names the summary lists.
MAX_SPAWNED_PROGRAMS = 12

#: ``python3.12`` is ``python`` for the purpose of "can this reach the network
#: unseen"; likewise a versioned ``node``/``ruby``/``perl``/``php``.
_VERSIONED = re.compile(r"^(python|node|ruby|perl|php)[0-9.]*$")


def _is_http(spawn: dict[str, Any]) -> bool:
    return (
        spawn.get("program") in HTTP_CLIS and bool(spawn.get("method")) and bool(spawn.get("url"))
    )


def _error_body(body: str) -> bool:
    """The general "refused" shape: a JSON object with a top-level ``error``.
    The mock's 500 and its 429 both look like this, and so does most of the
    web; a CLI that exited 0 cannot say otherwise, so the body has to."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and "error" in parsed


def merge_spawned_http(
    http_calls: list[dict[str, Any]] | None, spawns: list[dict[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """The recorder's HTTP and the spawned HTTP as ONE list, in the order the
    exchanges completed. ``None`` only when neither recorder left a record.

    A spawned entry carries ``via`` (the program), ``returncode`` and — when
    it exited 0 with no status line to read but its JSON body says ``error``
    — ``refused: True``, which :func:`act_observe.accepted_write` reads. Its
    ``status`` stays ``None``: a 200 is never invented from an exit code.
    """
    if http_calls is None and spawns is None:
        return None
    merged: list[dict[str, Any]] = list(http_calls or [])
    for spawn in spawns or []:
        if not _is_http(spawn):
            continue
        call: dict[str, Any] = {
            "method": spawn["method"],
            "url": spawn["url"],
            "status": spawn.get("status"),
            "body": spawn.get("body") or "",
            "truncated": bool(spawn.get("truncated")),
            "via": spawn["program"],
            "returncode": spawn.get("returncode"),
            "t": float(spawn.get("t") or 0.0),
        }
        if call["status"] is None and _error_body(call["body"]):
            call["refused"] = True
        merged.append(call)
    merged.sort(key=lambda c: float(c.get("t") or 0.0))  # stable: ties keep file order
    return merged


def spawn_summary(spawns: list[dict[str, Any]]) -> dict[str, Any]:
    """``spawned`` and, when it applies, ``egress_unobserved`` for the result.

    Counts the spawns that did NOT merge into ``http_calls``: the ones this
    result otherwise says nothing about. An HTTP CLI whose command line could
    not be read (no URL found) is in here too, and it is listed under
    ``egress_unobserved`` — it happened, it may have written, and nothing can
    name what it touched.
    """
    others = [s for s in spawns if not _is_http(s)]
    if not others:
        return {}
    programs: set[str] = set()
    unseen: set[str] = set()
    for spawn in others:
        name = str(spawn.get("program") or "")
        if not name:
            continue
        match = _VERSIONED.match(name)
        canonical = match.group(1) if match else name
        programs.add(canonical)
        if canonical in EGRESS_TOOLS or canonical in HTTP_CLIS:
            unseen.add(canonical)
    out: dict[str, Any] = {
        "spawned": {"count": len(others), "programs": sorted(programs)[:MAX_SPAWNED_PROGRAMS]}
    }
    if unseen:
        out["egress_unobserved"] = sorted(unseen)
    return out
