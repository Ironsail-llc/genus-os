"""The credential pool must be the ONLY door to a provider.

2026-08-27: one capped OpenRouter key degraded the fleet for 48 hours.
``key_pool.py`` retired it correctly and ``llm_client`` skipped every model
on it — and the error storm continued anyway, because eight other modules
dial the provider directly and never consulted the pool (seven remain;
``memory/generation.py`` was routed through the pool on the same day, and
was the highest-volume consumer at 1,135 remote fallbacks in 48h). A chokepoint that
guards one of seven doors is not a chokepoint.

Consequences of a bypass:

* the dead credential keeps being hammered after the pool has retired it
* the bypassing path cannot rotate to a spare, so adding one does not help
* the exhaustion alert never fires for traffic that never touched the pool

This is a RATCHET, not a wall. ``KNOWN_BYPASSES`` records the paths that
predate the pool; the set may shrink, never grow. A new direct dial is a
test failure, and removing one means deleting its line here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "robothor"

#: Modules that reach a provider without going through KeyPool.
#: Every entry is a known gap. Shrink this set; never extend it.
KNOWN_BYPASSES: set[str] = {
    "cli/codex.py",
}

#: The one module that is *supposed* to dial out, plus support modules that
#: reference the call without making one.
ALLOWED = {"engine/llm_client.py", "engine/codex_provider.py"}

#: A real outbound provider call. Deliberately narrow: "completion" is
#: also a domain word in this codebase (completion_contract, dashboards/
#: completions, crm/dal), and matching it loosely flags modules that never
#: touch a provider — which would make the ratchet noise and get muted.
_DIAL = re.compile(
    r"litellm\.a?completion\s*\("          # litellm, sync or async
    r"|(?<![\w.])await\s+acompletion\s*\("  # bare imported acompletion
    r"|Bearer \{os\.environ"                # raw HTTP auth header
)


#: Evidence that a dialer authenticates through the shared pool rather than
#: letting the provider SDK resolve the environment on its own.
_POOLED = re.compile(r"api_key_for_model|shared_pool|_key_pool\(|_remote_api_key")


def _dialers() -> set[str]:
    """Modules that reach a provider WITHOUT going through the pool.

    A module that dials and also consults the pool is not a bypass — that is
    the intended shape, and flagging it would make the ratchet unpassable and
    therefore ignored.
    """
    found = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel.startswith("tests/"):
            continue
        text = path.read_text(errors="ignore")
        dials = any(
            _DIAL.search(line)
            for line in text.splitlines()
            if not line.strip().startswith(("#", "*"))
        )
        if dials and not _POOLED.search(text):
            found.add(rel)
    return found


def test_no_new_provider_bypass_is_introduced():
    offenders = _dialers() - ALLOWED - KNOWN_BYPASSES
    assert not offenders, (
        "these modules dial a provider without going through KeyPool, so a "
        "retired credential keeps being hammered and a spare key cannot help:\n  "
        + "\n  ".join(sorted(offenders))
        + "\nRoute them through LLMClient, or add them to KNOWN_BYPASSES with "
        "a reason if that is genuinely impossible."
    )


def test_the_bypass_list_does_not_go_stale():
    """A fixed bypass must be removed from the list, or the ratchet slips."""
    still_bypassing = _dialers()
    fixed = {p for p in KNOWN_BYPASSES if p not in still_bypassing}
    assert not fixed, (
        "these no longer bypass the pool — delete them from KNOWN_BYPASSES so "
        f"the ratchet keeps its tension: {sorted(fixed)}"
    )


def test_llm_client_is_actually_pooled():
    """The one allowed dialer must consult the pool."""
    text = (ROOT / "engine" / "llm_client.py").read_text()
    assert "_key_pool(" in text and "pool.exhausted()" in text
