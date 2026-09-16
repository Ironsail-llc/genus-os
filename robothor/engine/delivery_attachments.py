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

#: Keyed by run so two concurrent nightly reports cannot deliver each other's
#: PDF, and drained on collection so a retried run never inherits the previous
#: attempt's files.
_queued: dict[str, list[tuple[str, str]]] = {}

#: The most files one run may queue. A run in a loop writing charts must not be
#: able to turn one announcement into a hundred uploads.
MAX_QUEUED_ATTACHMENTS = 10


def queue_attachment(run_id: str, path: str, caption: str = "") -> bool:
    """Park a file to be delivered with ``run_id``'s announcement.

    Returns False when the run has already queued :data:`MAX_QUEUED_ATTACHMENTS`
    — a refusal the caller reports to the agent, rather than a silent drop.
    """
    if not run_id or not path:
        return False
    queue = _queued.setdefault(str(run_id), [])
    if len(queue) >= MAX_QUEUED_ATTACHMENTS:
        return False
    queue.append((str(path), str(caption or "")))
    return True


def take_queued_attachments(run_id: str) -> list[tuple[str, str]]:
    """Everything queued for ``run_id``, removing it from the queue."""
    return _queued.pop(str(run_id), [])


def clear_queued_attachments() -> None:
    """Drop every queued attachment. For tests and for a clean daemon restart."""
    _queued.clear()


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
    pending = [(path, "") for path in (getattr(run, "attachments", None) or [])]
    pending.extend(take_queued_attachments(run.id))
    if not pending:
        return

    target = config.delivery_to
    for path, caption in pending[:MAX_QUEUED_ATTACHMENTS]:
        if not Path(path).is_file():
            logger.warning(
                "Attachment for %s is missing at delivery time: %s", config.id, Path(path).name
            )
            continue
        try:
            receipt = await channel.send_attachment(target, path, caption)
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
