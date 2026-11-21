"""Golden replies shared by the servers' tests and the dashboard's.

The dashboard's Vitest parses the replies the bridge and the engine REALLY
give (``app/src/lib/rooms/__tests__/contract/*.json``), and the servers' tests
record them, so a change on either side fails a test:

* a server whose reply changes fails ``check_golden`` until the file is
  regenerated (``ROOMS_CONTRACT_UPDATE=1``), which shows the change in review;
* a dashboard parser that no longer reads a recorded reply fails its Vitest.

Recorded replies are made stable: every UUID becomes a name (the caller's
``names``, else ``id-N`` in order of first appearance), every timestamp
``<time>``, and every integer under a key in ``_COUNTERS`` ``<n>``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = ROOT / "app" / "src" / "lib" / "rooms" / "__tests__" / "contract"
UPDATE_ENV = "ROOMS_CONTRACT_UPDATE"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
#: Integers that differ from run to run (database sequences).
_COUNTERS = frozenset({"id", "messageId", "lastMessageId", "serverId", "before"})


class Normaliser:
    """Stable names for the ids and times of one recording."""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.names = dict(names or {})
        self._next = 0

    def _id(self, value: str) -> str:
        if value not in self.names:
            self._next += 1
            self.names[value] = f"id-{self._next}"
        return self.names[value]

    def text(self, value: str) -> str:
        value = _UUID.sub(lambda m: self._id(m.group(0)), value)
        return _TIME.sub("<time>", value)

    def __call__(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: self(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, int) and not isinstance(value, bool) and key in _COUNTERS:
            return "<n>"
        return value


def check_golden(name: str, recorded: dict[str, Any]) -> None:
    """Compare ``recorded`` with the committed golden file ``name``, or write it.

    The file is JSON with sorted keys so a regeneration diffs cleanly.
    """
    path = GOLDEN_DIR / name
    text = json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get(UPDATE_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no golden file {path.name}: run with {UPDATE_ENV}=1 to record it"
    committed = json.loads(path.read_text(encoding="utf-8"))
    fresh = json.loads(text)
    changed = sorted(k for k in set(committed) | set(fresh) if committed.get(k) != fresh.get(k))
    assert not changed, (
        f"the server's replies changed for {changed} in {path.name}; if that is intended, "
        f"run with {UPDATE_ENV}=1, commit the file and check the dashboard's contract test"
    )
