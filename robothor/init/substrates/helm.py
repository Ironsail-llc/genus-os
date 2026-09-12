"""A Kubernetes cluster via the Helm chart. Not built yet.

The design is in the productization spec (workstream A, A12): render
``values-instance.yaml``, a secrets script or Vault path list, and a
workspace-seed ConfigMap carrying ``docs/agents/main.yaml`` (the chart's
``/ready`` requires it), then print the ``helm upgrade --install`` and
port-forward commands.
"""

from __future__ import annotations

from typing import NoReturn

__all__ = ["substrate"]


def substrate() -> NoReturn:
    raise NotImplementedError("lands in A10/A11/A12")
