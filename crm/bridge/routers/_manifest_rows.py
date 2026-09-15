"""A manifest document, as the row a list view shows.

Extracted from ``agent_manifests.py`` when adding ``chattable`` pushed that
module past its decomposition ratchet. It is a real cluster rather than a place
to put spare lines: everything here turns a PARSED manifest (or a scan failure)
into the small dict a browser renders, it touches no filesystem, no engine and
no request, and it has a second caller already — ``automations.py`` reads
``_summary`` and ``_block`` so the cron it shows is the same cron the fleet
list shows, rather than a second opinion about the same file.

Rule 1 and rule 2 apply here as hard as anywhere in the router: a row carries
ids, dotted schema paths and error TYPES. Never a filesystem path, never a
parser message that quotes one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _block(document: dict[str, Any], key: str) -> dict[str, Any]:
    """One nested block of a manifest, or ``{}`` if it is not a mapping.

    ``document.get(key) or {}`` is the version that looks right and is not: a
    FALSY non-mapping (``[]``, ``""``) is caught by the ``or`` and a truthy one
    (``"0 9 * * *"``, ``[announce]``) sails straight into ``.get()``. That is an
    `AttributeError` out of a route, and a manifest whose `model:` is a bare
    string is exactly the kind a human hand-edited and needs the editor for.
    """
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def _summary(document: dict[str, Any]) -> dict[str, Any]:
    """The fields one fleet-list row shows.

    A whole manifest per row would put every agent's tool list, warmup files
    and delivery target into a single response the list view never reads.

    Shared with ``automations.py``, which is why the chat verdict is NOT here:
    see :func:`_chat_fields`.
    """
    schedule = _block(document, "schedule")
    delivery = _block(document, "delivery")
    model = _block(document, "model")
    return {
        "id": str(document.get("id") or ""),
        "name": document.get("name") or "",
        "description": document.get("description") or "",
        "version": str(document.get("version") or ""),
        "department": document.get("department") or "",
        "cron": schedule.get("cron") or "",
        "timezone": schedule.get("timezone") or "",
        "enabled": bool(schedule.get("enabled", True)),
        "delivery": delivery.get("mode") or "none",
        "model": model.get("primary") or "",
    }


def _chat_fields(document: dict[str, Any], main_agent_id: str) -> dict[str, Any]:
    """Whether the Helm's chat may address this agent, and on what evidence.

    Separate from :func:`_summary` rather than a defaulted parameter on it,
    because ``_summary`` has a second caller. ``automations.py`` has no
    main-agent id to pass and no use for the verdict, so a default argument
    there would have shipped ``chattable: false`` into every automations row —
    including the row for the one agent that is always chattable. A wrong
    answer nobody asked for is worse than no answer, and a default argument is
    how it travels.

    Addressing an agent is nothing more than a ``session_key`` of
    ``agent:<id>:primary``, but a key is only a CONVERSATION if the agent holds
    its session between runs — ``schedule.session_target: persistent``. An
    ``isolated`` worker starts fresh every run, so a reply would come from
    somebody who has already forgotten the question.

    The main agent is chattable whatever its manifest says: its schedule block
    describes a heartbeat, while the session the operator has been talking to
    all along is the one ``EngineConfig.main_session_key`` pins. The caller
    passes that id in; it is never spelled ``"main"`` here.

    ``session_target`` reports what the manifest SAYS, empty included. Filling
    in the engine's ``isolated`` default would put the engine's opinion in a
    field an operator reads as their own words.
    """
    agent_id = str(document.get("id") or "")
    session_target = str(_block(document, "schedule").get("session_target") or "")
    return {
        "session_target": session_target,
        "chattable": session_target == "persistent"
        or (bool(agent_id) and agent_id == main_agent_id),
    }


def _broken(scan: Any) -> list[dict[str, str]]:
    """The failure bucket, as ids and error TYPES.

    ``ManifestFailure.detail`` is a parser message and routinely carries the
    absolute path of the file; it does not come out here.
    """
    return [
        {
            "id": failure.agent_id or Path(failure.filename).stem,
            "filename": failure.filename,
            "error_type": failure.error_type,
        }
        for failure in scan.failures
    ]
