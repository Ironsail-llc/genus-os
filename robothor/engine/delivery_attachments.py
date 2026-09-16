"""Files that ride out with a run's announcement.

An interactive agent sends a file the moment it has one: there is a person on
the other end of the run and ``send_file`` reaches them. A scheduled agent has
nobody watching while it works — its output arrives later as an announcement —
so a file it made has to travel WITH that announcement or not at all. Sending
it the moment it was written would put a PDF in the operator's chat minutes
before the report that explains it, or hours before nothing at all if the run
then failed.

A tool handler holds the run ID and nothing else — no reference to the
``AgentRun`` object — so ``send_file`` parks its file in the queue here and
``delivery.deliver()`` collects it by ID on the way out.

Its own module rather than another 90 lines in ``delivery.py``: this is a
cohesive cluster (a queue, its bound, and the send that drains it) with one
caller, which is exactly what the decomposition ratchet asks for instead of
growing the god-object one feature at a time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_QUEUED_ATTACHMENTS",
    "clear_queued_attachments",
    "queue_attachment",
    "send_attachments",
    "take_queued_attachments",
]


@dataclass(frozen=True)
class QueuedAttachment:
    """One file a run wants delivered, pinned to what was approved.

    ``digest`` and ``workspace`` are the evidence: the ladder ran against THESE
    bytes, inside THAT tree. Both are re-checked at send time, because the file
    on disk when the channel opens it need not be the file the agent queued —
    that is exactly the hole the hostile review walked through.
    """

    path: str
    caption: str = ""
    digest: str = ""
    workspace: str = ""


#: Keyed by run so two concurrent nightly reports cannot deliver each other's
#: PDF, and drained on collection so a retried run never inherits the previous
#: attempt's files.
_queued: dict[str, list[QueuedAttachment]] = {}

#: The most files one run may queue. A run in a loop writing charts must not be
#: able to turn one announcement into a hundred uploads.
MAX_QUEUED_ATTACHMENTS = 10


def queue_attachment(
    run_id: str,
    path: str,
    caption: str = "",
    *,
    digest: str = "",
    workspace: str = "",
) -> bool:
    """Park a file to be delivered with ``run_id``'s announcement.

    ``digest`` is the content hash the ladder approved. It is re-checked at send
    time and a mismatch is refused as ``changed_since_queued`` — an empty digest
    means nothing was pinned, and the ladder still runs, it just cannot tell a
    rewritten file from the original.

    Returns False when the run has already queued :data:`MAX_QUEUED_ATTACHMENTS`
    — a refusal the caller reports to the agent, rather than a silent drop.
    """
    if not run_id or not path:
        return False
    queue = _queued.setdefault(str(run_id), [])
    if len(queue) >= MAX_QUEUED_ATTACHMENTS:
        return False
    queue.append(
        QueuedAttachment(
            path=str(path),
            caption=str(caption or ""),
            digest=str(digest or ""),
            workspace=str(workspace or ""),
        )
    )
    return True


def take_queued_attachments(run_id: str) -> list[QueuedAttachment]:
    """Everything queued for ``run_id``, removing it from the queue."""
    return _queued.pop(str(run_id), [])


def clear_queued_attachments() -> None:
    """Drop every queued attachment. For tests and for a clean daemon restart."""
    _queued.clear()


def _refuse(item: QueuedAttachment) -> str | None:
    """Re-run the send ladder for one queued file, or say why it may not go.

    The workspace is the one the file was approved against; without one there is
    no tree to judge containment against and the file is refused rather than
    sent on trust.
    """
    from robothor.engine.attachment_gate import refuse_to_send

    root = item.workspace
    if not root:
        try:
            from robothor.settings.sources import workspace_path

            resolved = workspace_path()
            root = str(resolved) if resolved is not None else ""
        except Exception:  # noqa: BLE001 - an unresolvable workspace refuses
            root = ""
    if not root:
        return "no resolvable workspace to judge containment against"
    return refuse_to_send(item.path, root, expect_digest=item.digest or None)


async def send_attachments(config: AgentConfig, run: AgentRun, name: str, channel: Any) -> None:
    """Send the files this run wants delivered with its announcement.

    Never changes the run's delivery status and never raises. The report and
    the files are separate sends: one failing is not evidence about the other,
    and an operator holding the PDF with no covering note is better off than
    one holding neither.

    A missing file is logged and skipped — the run wrote a path that is no
    longer there, which is worth knowing and is not worth failing a delivered
    report over. A channel that cannot attach files raises
    ``NotImplementedError`` by contract (``channels/base.py``), and that is
    recorded here rather than allowed to escape into run finalization, where it
    would look like the run itself failed.
    """
    # `run.attachments` carries bare paths that NOTHING has ever checked — an
    # agent (or a hook) can set the list directly. They go through the same
    # ladder, unpinned, which is the only honest thing to do with a path whose
    # provenance is unknown.
    pending = [QueuedAttachment(path=path) for path in (getattr(run, "attachments", None) or [])]
    pending.extend(take_queued_attachments(run.id))
    if not pending:
        return

    target = config.delivery_to
    for item in pending[:MAX_QUEUED_ATTACHMENTS]:
        path = item.path
        if not Path(path).is_file():
            logger.warning(
                "Attachment for %s is missing at delivery time: %s", config.id, Path(path).name
            )
            continue

        # THE LADDER, AGAIN, HERE. Everything `send_file` checked ran against
        # the bytes that were on disk when the agent called the tool; the
        # channel opens the file now. A run that queued a benign CSV and then
        # overwrote it with an AWS secret key shipped the key.
        refusal = _refuse(item)
        if refusal:
            logger.error(
                "Attachment %s for %s was refused at delivery: %s",
                Path(path).name,
                config.id,
                refusal,
            )
            continue

        try:
            receipt = await channel.send_attachment(target, path, item.caption)
        except NotImplementedError as e:
            logger.warning(
                "Channel %s cannot deliver the file %s for %s: %s",
                name,
                Path(path).name,
                config.id,
                e,
            )
            continue
        except Exception as e:  # noqa: BLE001 - a third-party channel is third-party code
            logger.error("Channel %s raised sending %s for %s: %s", name, path, config.id, e)
            continue
        if receipt.acknowledged <= 0:
            logger.error(
                "Attachment %s for %s was not acknowledged — the operator did not get it",
                Path(path).name,
                config.id,
            )
