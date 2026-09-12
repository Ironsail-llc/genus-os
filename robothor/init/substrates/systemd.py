"""systemd units on a host checkout. Not built yet.

The design is in the productization spec (workstream A, A11):
``scripts/install-units.sh --profile core|all`` rather than all sixty-odd
templates, with secrets decoupled from SOPS through
``scripts/load-secrets.sh``.
"""

from __future__ import annotations

from typing import NoReturn

__all__ = ["substrate"]


def substrate() -> NoReturn:
    raise NotImplementedError("lands in A10/A11/A12")
