"""One ``claude -p`` invocation: argv, the stream-json parser, the subprocess.

Modelled on :mod:`robothor.engine.codex_provider`: ``create_subprocess_exec``
with no shell, the prompt over stdin (argv is readable by every process of the
same uid through ``/proc``), and a timeout that kills rather than abandons.

Two differences from the Codex path, both deliberate:

* **The output is a stream, not a file.** ``--output-format stream-json
  --verbose`` emits one JSON object per line — ``system/init`` (carrying the
  session id), ``assistant`` and ``user`` turns, and a final ``result`` with
  cost, turns and the answer. The parser turns the turns into short progress
  events, so a job's status can say what Claude Code is doing *now*, and keeps
  the session id the moment it is announced, so a crash mid-round can still be
  resumed with ``--resume``.
* **The child runs in its own process group.** Claude Code spawns shells, test
  runners and language servers; killing only the direct child on timeout would
  leave those running against the worktree. ``start_new_session=True`` makes
  the whole tree killable with one ``killpg``.

Isolation from the operator's own configuration is in the argv, not in a hope:
``--setting-sources ""`` loads no user, project or local settings file and
``--strict-mcp-config`` loads no MCP server that was not passed explicitly
(none is). ``--permission-mode dontAsk`` denies anything not on the allowed
list instead of waiting on a prompt nobody will answer. ``--settings`` carries
the job's sandbox block (:mod:`robothor.engine.coding.sandbox`); it is an
explicit flag, so ``--setting-sources ""`` does not drop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ClaudeCodeError",
    "ClaudeInvocation",
    "ClaudeResult",
    "ProgressEvent",
    "StreamParser",
    "EFFORTS",
    "MODES",
    "build_argv",
    "check_effort",
    "check_mode",
    "resolve_claude_binary",
    "run_claude",
]

#: A session id is a UUID. Checked before it reaches argv so that a corrupted
#: row cannot smuggle a flag (``--dangerously-skip-permissions``) in its place.
_SESSION_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

#: stream-json lines carry whole tool results; the default 64 KiB StreamReader
#: limit would turn one large file read into a crashed job.
_LINE_LIMIT = 32 * 1024 * 1024

#: How much of stderr is kept for the error summary.
_STDERR_TAIL = 4000

#: Grace between SIGTERM and SIGKILL for the process group.
_KILL_GRACE_SECONDS = 5.0

#: Characters of one progress event. Status shows the last few; a whole file
#: body in a status line helps nobody.
_EVENT_CHARS = 240


class ClaudeCodeError(RuntimeError):
    """The CLI could not be run at all."""


#: The job modes. Their tool rules and sandbox are built per job, from the
#: job's own paths, in :mod:`robothor.engine.coding.sandbox`.
MODES: tuple[str, ...] = ("code", "review", "readonly")


#: ``claude --effort`` levels (Claude Code 2.1.289+). Checked before argv.
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


def check_effort(effort: str | None) -> str | None:
    """``effort`` when it is empty or one of :data:`EFFORTS`, else ``ValueError``."""
    if effort is None or effort == "":
        return None
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {', '.join(EFFORTS)}")
    return effort


def check_mode(mode: str) -> str:
    """``mode`` when it is one of :data:`MODES`, else ``ValueError``."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    return mode


# ── argv ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClaudeInvocation:
    """Everything one ``claude -p`` call needs, and nothing it may not have."""

    prompt: str
    cwd: Path | str
    binary: str | None = None
    model: str | None = None
    #: ``--effort`` level; None leaves the CLI's default.
    effort: str | None = None
    permission_mode: str = "dontAsk"
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    max_turns: int | None = None
    max_budget_usd: float | None = None
    append_system_prompt: str | None = None
    json_schema: dict[str, Any] | None = None
    resume_session_id: str | None = None
    #: The ``--settings`` object (the job's sandbox block). None passes none.
    settings: dict[str, Any] | None = None


def resolve_claude_binary() -> str:
    """The Claude Code CLI to run: ``ROBOTHOR_CLAUDE_BIN``, PATH, then ~/.local/bin.

    The last fallback exists because the native installer puts the binary in
    ``~/.local/bin``, which a systemd unit's PATH usually does not include.
    """
    from robothor.settings import get_settings

    configured = get_settings().coding.claude_bin.strip()
    candidate = configured or "claude"
    found = shutil.which(candidate)
    if found:
        return found
    if not configured:
        local = Path.home() / ".local" / "bin" / "claude"
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    raise ClaudeCodeError(
        f"Claude Code CLI not found ({candidate}). Install it for the engine's service "
        "user or set ROBOTHOR_CLAUDE_BIN."
    )


def build_argv(inv: ClaudeInvocation) -> list[str]:
    """The argv for one headless, stream-json, config-isolated call."""
    argv = [
        inv.binary or resolve_claude_binary(),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--permission-mode",
        inv.permission_mode,
    ]
    if inv.settings is not None:
        argv += ["--settings", json.dumps(inv.settings, separators=(",", ":"))]
    if inv.model:
        argv += ["--model", inv.model]
    effort = check_effort(inv.effort)
    if effort:
        argv += ["--effort", effort]
    if inv.allowed_tools:
        argv += ["--allowedTools", ",".join(inv.allowed_tools)]
    if inv.disallowed_tools:
        argv += ["--disallowedTools", ",".join(inv.disallowed_tools)]
    if inv.max_turns:
        argv += ["--max-turns", str(int(inv.max_turns))]
    if inv.max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{float(inv.max_budget_usd):.2f}"]
    if inv.append_system_prompt:
        argv += ["--append-system-prompt", inv.append_system_prompt]
    if inv.json_schema is not None:
        argv += ["--json-schema", json.dumps(inv.json_schema, separators=(",", ":"))]
    if inv.resume_session_id:
        if not _SESSION_RE.match(inv.resume_session_id):
            raise ValueError(f"not a session id: {inv.resume_session_id!r}")
        argv += ["--resume", inv.resume_session_id]
    return argv


# ── The stream ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProgressEvent:
    """One thing Claude Code did, short enough to show in a status line."""

    kind: str  # init | text | tool_use | tool_result | result
    summary: str
    session_id: str | None = None

    def line(self) -> str:
        return f"{self.kind}: {self.summary}"


@dataclass
class ClaudeResult:
    """What one call came back with. ``is_error`` is Claude Code's own verdict."""

    session_id: str | None
    is_error: bool
    subtype: str
    result_text: str
    total_cost_usd: float
    num_turns: int
    structured_output: Any = None
    permission_denials: list[Any] = field(default_factory=list)
    exit_code: int | None = 0
    timed_out: bool = False
    stderr_tail: str = ""
    #: False when the call ended (killed, timed out, crashed) before Claude
    #: Code's own ``result`` line: ``total_cost_usd`` is then unknown, not 0.
    got_result: bool = True

    @property
    def error_summary(self) -> str:
        """Why this call failed, in one line, or "" when it did not."""
        if not self.is_error:
            return ""
        parts = []
        if self.timed_out:
            parts.append("timed out")
        if self.subtype and self.subtype != "success":
            parts.append(self.subtype)
        if self.exit_code not in (0, None):
            parts.append(f"exit {self.exit_code}")
        if self.result_text:
            parts.append(self.result_text[:300])
        if self.stderr_tail:
            parts.append(self.stderr_tail[-300:])
        return "; ".join(parts) or "claude reported an error"


def _clip(text: str, limit: int = _EVENT_CHARS) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _summarise_tool_input(name: str, raw: Any) -> str:
    if not isinstance(raw, dict):
        return _clip(str(raw))
    for key in ("command", "file_path", "path", "pattern", "url", "description"):
        if raw.get(key):
            return _clip(str(raw[key]))
    return _clip(json.dumps(raw, default=str))


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


class StreamParser:
    """Turns stream-json lines into progress events and one final result."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None
        self._result: dict[str, Any] | None = None

    def feed(self, line: str) -> list[ProgressEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            data = json.loads(line)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            self.session_id = sid
        kind = data.get("type")

        if kind == "system" and data.get("subtype") == "init":
            self.model = str(data.get("model") or "") or None
            return [ProgressEvent("init", f"session started ({self.model or 'model?'})", sid)]

        events: list[ProgressEvent] = []
        if kind == "assistant":
            message = data.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    events.append(ProgressEvent("text", _clip(block["text"]), sid))
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    summary = f"{name}: {_summarise_tool_input(name, block.get('input'))}"
                    events.append(ProgressEvent("tool_use", _clip(summary), sid))
        elif kind == "user":
            message = data.get("message") or {}
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    prefix = "error" if block.get("is_error") else "ok"
                    events.append(
                        ProgressEvent(
                            "tool_result", _clip(f"{prefix}: {_tool_result_text(block)}"), sid
                        )
                    )
        elif kind == "result":
            self._result = data
            events.append(ProgressEvent("result", str(data.get("subtype") or "result"), sid))
        return events

    def finish(self, *, exit_code: int | None, stderr_tail: str, timed_out: bool) -> ClaudeResult:
        """The call's result — or an error result when no ``result`` line came."""
        data = self._result
        if data is None:
            return ClaudeResult(
                session_id=self.session_id,
                is_error=True,
                subtype="no_result",
                result_text="",
                total_cost_usd=0.0,
                num_turns=0,
                exit_code=exit_code,
                timed_out=timed_out,
                stderr_tail=stderr_tail,
                got_result=False,
            )
        is_error = bool(data.get("is_error")) or timed_out or exit_code not in (0, None)
        return ClaudeResult(
            session_id=str(data.get("session_id") or self.session_id or "") or None,
            is_error=is_error,
            subtype=str(data.get("subtype") or ""),
            result_text=str(data.get("result") or ""),
            total_cost_usd=float(data.get("total_cost_usd") or 0.0),
            num_turns=int(data.get("num_turns") or 0),
            structured_output=data.get("structured_output"),
            permission_denials=list(data.get("permission_denials") or []),
            exit_code=exit_code,
            timed_out=timed_out,
            stderr_tail=stderr_tail,
        )


# ── The subprocess ────────────────────────────────────────────────────


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the process group, then SIGKILL whatever is left."""
    if proc.returncode is not None:
        # The leader is gone, but its group may not be.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), _KILL_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), _KILL_GRACE_SECONDS)


async def run_claude(
    inv: ClaudeInvocation,
    *,
    env: dict[str, str],
    timeout_s: float,
    on_event: Callable[[ProgressEvent], None] | None = None,
) -> ClaudeResult:
    """Run one ``claude -p`` call to completion, timeout, or cancellation.

    A timeout kills the process group and returns an error result carrying the
    session id, so the caller can resume. Cancellation kills it too and
    re-raises: the caller decides what a cancellation means.
    """
    argv = build_argv(inv)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(inv.cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=_LINE_LIMIT,
    )
    parser = StreamParser()
    stderr_tail = ""

    def _emit(event: ProgressEvent) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a status hook must never kill the job
            logger.debug("claude progress hook failed", exc_info=True)

    async def _feed_stdin() -> None:
        assert proc.stdin is not None
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(inv.prompt.encode())
            await proc.stdin.drain()
        with contextlib.suppress(Exception):
            proc.stdin.close()

    async def _read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:  # a single line over the limit: skip it
                continue
            if not raw:
                return
            for event in parser.feed(raw.decode(errors="replace")):
                _emit(event)

    async def _read_stderr() -> None:
        nonlocal stderr_tail
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_tail = (stderr_tail + chunk.decode(errors="replace"))[-_STDERR_TAIL:]

    timed_out = False
    try:
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(_feed_stdin(), _read_stdout(), _read_stderr())
            await proc.wait()
    except TimeoutError:
        timed_out = True
        logger.warning("claude -p timed out after %ss in %s; killing", timeout_s, inv.cwd)
        await _kill_group(proc)
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    else:
        # Background children of a finished session must not outlive it.
        await _kill_group(proc)

    return parser.finish(exit_code=proc.returncode, stderr_tail=stderr_tail, timed_out=timed_out)
