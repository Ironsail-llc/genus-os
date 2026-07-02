"""Compat shim — the log sanitizer moved to ``robothor.sanitize`` (a neutral
bottom layer) so the CRM data layer can use it without importing engine code
(see tests/test_import_boundaries.py). Existing ``robothor.engine.sanitize``
importers keep working via this re-export."""

from __future__ import annotations

from robothor.sanitize import sanitize_log

__all__ = ["sanitize_log"]
