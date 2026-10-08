"""Platform ingestion: provider mail and calendar changes -> events on the bus.

* :mod:`~robothor.workspace.ingest.base` -- the provider-neutral
  :class:`Ingestor`, its :class:`IngestReport` and the bus publisher;
* :mod:`~robothor.workspace.ingest.microsoft` -- Microsoft 365 over Graph
  delta queries (assistant inbox, owner calendar);
* :mod:`~robothor.workspace.ingest.state` -- delta positions, high-water
  marks and the seen set (migration 149);
* :mod:`~robothor.workspace.ingest.email_log` -- the atomic, merging
  ``email-log.json`` writer;
* :mod:`~robothor.workspace.ingest.worker` -- the daemon worker, dark unless
  ``workspace_provider = microsoft365``.

The events are the ones the Google sync scripts publish, by the contract in
:mod:`robothor.events.contract`. See ``docs/workspace/microsoft365.md``.
"""

from __future__ import annotations

from robothor.workspace.ingest.base import Ingestor, IngestReport, Publisher, bus_publisher

__all__ = ["IngestReport", "Ingestor", "Publisher", "bus_publisher"]
