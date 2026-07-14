"""A guardrail event that fails to record must never be silent.

The whole enforcement ladder rests on agent_guardrail_events: it is the soak
evidence, the audit trail, and the operator's only view of what a control did.
When those writes are wrapped in `contextlib.suppress(Exception)`, a failure
disappears — which is exactly how enforce-mode injection blocks became
invisible (#184) and how the exec-allowlist soak read "clean" while recording
nothing (#187).

Every log_guardrail_event() call site must therefore sit in a try/except that
LOGS the failure, not a blanket suppress.
"""

from __future__ import annotations

import re
from pathlib import Path

import robothor.engine.runner as runner_mod

SOURCES = [
    Path(runner_mod.__file__),
    Path(runner_mod.__file__).parent / "guardrails.py",
    Path(runner_mod.__file__).parent.parent / "memory" / "drift.py",
]


def _preceding_block(src: str, pos: int, lines: int = 4) -> str:
    start = src.rfind("\n", 0, pos)
    for _ in range(lines):
        start = src.rfind("\n", 0, start - 1)
        if start == -1:
            return src[:pos]
    return src[start:pos]


def test_no_guardrail_event_write_is_blanket_suppressed():
    offenders = []
    for path in SOURCES:
        if not path.exists():
            continue
        src = path.read_text()
        for m in re.finditer(r"log_guardrail_event\(", src):
            window = _preceding_block(src, m.start())
            if "contextlib.suppress(Exception)" in window and "try:" not in window:
                line = src[: m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")

    assert not offenders, (
        "these guardrail-event writes sit under a blanket contextlib.suppress — "
        "if the write fails (an FK violation, a dead connection), the control "
        "fires and leaves no trace, and the soak reports 'clean'. Use an "
        f"explicit try/except that logs at error level: {offenders}"
    )
