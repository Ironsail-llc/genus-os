"""Run-local failed endpoint exclusions; each subsequent attempt needs a fresh quote."""

import threading
from copy import deepcopy
from typing import Any


class RequestRoutes:
    def __init__(self) -> None:
        self._failed: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def for_quote(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            failed = sorted(self._failed.get(kwargs.get("model", ""), set()))
        if not failed:
            return kwargs
        extra = deepcopy(kwargs.get("extra_body") or {})
        routing = extra.setdefault("provider", {})
        routing["ignore"] = list(dict.fromkeys([*(routing.get("ignore") or []), *failed]))
        return {**kwargs, "extra_body": extra}

    @staticmethod
    def identity(kwargs: dict[str, Any]) -> tuple[str, str] | None:
        model = kwargs.get("model", "")
        routing = (kwargs.get("extra_body") or {}).get("provider") or {}
        only = routing.get("only") or []
        if not model.startswith("openrouter/") or len(only) != 1 or not isinstance(only[0], str):
            return None
        return model, only[0]

    def failed(self, identity: tuple[str, str] | None, error: BaseException) -> None:
        if identity is None or not (
            isinstance(error, TimeoutError)
            or getattr(error, "status_code", None) in (429, 500, 502, 503, 504)
        ):
            return
        model, tag = identity
        with self._lock:
            self._failed.setdefault(model, set()).add(tag)
