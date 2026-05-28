"""execute_code sandbox foundation (Rip 6).

This module pins the resource caps, allowed-tools whitelist, and
sandbox-output schema that the v2 UDS-based runner will compose
around. The full subprocess + AF_UNIX RPC machinery
(robothor.engine.tools.handlers.code_execution_rpc + _client) is
genuinely security-sensitive and lands in a follow-up focused
session; shipping a half-finished sandbox is worse than no sandbox.

What this commit gives downstream:

* Constants matching Hermes ``tools/code_execution_tool.py:60-74``
  — same caps every Hermes user has battle-tested in production.
* Allowed-tools whitelist — only the side-effecting operations the
  Python author needs (no terminal-arbitrary-cmd, no MCP, no spawn).
* Result dataclass with the exact field shape the runner step
  recorder expects so step persistence doesn't have to change when
  v2 lands.

The model-facing tool will return ``{"error": "execute_code not yet
available"}`` until v2 ships, gated on ``ROBOTHOR_RIP_6_ENABLED``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Resource caps — ported from Hermes code_execution_tool.py:71-74 ─

DEFAULT_TIMEOUT_SECONDS = 300
"""Per-execute_code call wall-clock cap. 5 minutes covers the longest
legitimate Python orchestration the model is likely to write (multi-
file analysis, batch transforms); past this it's almost certainly a
runaway loop or a hung subprocess."""

DEFAULT_MAX_TOOL_CALLS = 50
"""Cap on RPC tool calls the sandboxed script can make in one run.
Beyond this is symptomatic of an infinite-loop pattern; cheap to
enforce, expensive to misdiagnose without."""

MAX_STDOUT_BYTES = 50_000
"""Stdout cap returned to the LLM context. Output past this is
truncated with an explicit `[truncated]` marker."""

MAX_STDERR_BYTES = 10_000
"""Stderr cap. Tighter than stdout because the model rarely needs
the full traceback verbatim; the head is what helps."""


# ── Allowed tools the sandboxed script may RPC-call ─────────────────
# Mirrors Hermes ``SANDBOX_ALLOWED_TOOLS`` (code_execution_tool.py:60).
# Note: terminal IS allowed because the whole point of execute_code
# is fast iteration; if the model needs to shell out, it should
# still be allowed to. The sandbox boundary is the subprocess, not
# this whitelist.
SANDBOX_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "web_search",
        "web_extract",
        "read_file",
        "write_file",
        "search_files",
        "patch",
        "terminal",
    }
)


@dataclass
class SandboxResult:
    """Structured output of one execute_code run.

    Mirrors the shape Hermes returns to the LLM so the runner's
    step recorder doesn't need a schema migration when the full
    sandbox lands.
    """

    stdout: str
    stderr: str
    returncode: int
    tool_call_count: int
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "tool_call_count": self.tool_call_count,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }


def truncate_with_marker(payload: bytes | str, max_bytes: int) -> tuple[str, bool]:
    """Cap output bytes and append a marker. Returns (text, was_truncated)."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if len(payload.encode("utf-8")) <= max_bytes:
        return payload, False
    # Find a cutoff that respects utf-8 boundaries.
    encoded = payload.encode("utf-8")[:max_bytes]
    truncated = encoded.decode("utf-8", errors="ignore")
    return f"{truncated}\n[truncated {len(payload) - len(truncated)} chars]", True
