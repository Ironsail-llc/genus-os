"""Observe a form-step transition without treating it as final submission."""

from __future__ import annotations

from typing import Any


def signature(inspection: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (field.get("frame_origin", ""), field.get("frame_selector", ""), field["selector"])
        for field in inspection["fields"]
        if field["tag"] in {"input", "select", "textarea"}
        and field.get("type") not in {"button", "submit", "reset", "hidden"}
    }
