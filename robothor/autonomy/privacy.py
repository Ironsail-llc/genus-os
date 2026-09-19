"""Workflow-local masking of values already entrusted to its protected browser."""

from __future__ import annotations

import base64
import html
from typing import Any
from urllib.parse import quote, quote_plus


class ProtectedValues:
    """Never serialized or shared across workflows; discarded with the browser.

    Inspection also switches to structural selectors, so a merchant-controlled
    element ID cannot export a CSS-escaped credential or produce a broken
    selector when its ID is masked.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def __bool__(self) -> bool:
        return bool(self._values)

    def add(self, value: str) -> None:
        if not value:
            return
        self._values.update(
            (
                value,
                quote(value, safe=""),
                quote(value, safe="-_.!~*'()"),
                quote_plus(value, safe=""),
                html.escape(value),
            )
        )
        self._values.add(base64.b64encode(value.encode()).decode())

    def session(self, storage: dict[str, Any] | None) -> None:
        if not storage:
            return
        for cookie in storage.get("cookies", []):
            self.add(str(cookie.get("value", "")))
        for origin in storage.get("origins", []):
            for item in origin.get("localStorage", []):
                self.add(str(item.get("value", "")))

    def text(self, value: str) -> str:
        for secret in sorted(self._values, key=len, reverse=True):
            value = value.replace(secret, "[private]")
        return value

    def clear(self) -> None:
        self._values.clear()
