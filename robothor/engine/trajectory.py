"""Trajectory capture — Rip 10.

Persist a completed run's full message transcript as one JSONL
ShareGPT-format file. Adapted from Hermes ``agent/trajectory.py``
(whole 56-line file). The artifacts have two uses:

* Offline replay for debugging (the goal of a transcript snapshot).
* Future fine-tuning corpus for tool-calling models — the ShareGPT
  ``{from, value}`` shape is the format every open-weights tuner
  expects.

Files land at ``workspace/trajectories/<tenant>/<YYYY-MM>/<file>.jsonl``::

  trajectory_samples.jsonl     # completed runs
  failed_trajectories.jsonl    # errored / aborted runs

Sampling is governed by ``ROBOTHOR_TRAJECTORY_SAMPLE`` (a float in
``[0.0, 1.0]``; default ``0.0`` = never). The runner hooks
``save_trajectory_for_run`` into ``_after_response_delivered``; the
function itself is the one place that consults the sample rate and
returns silently when the dice say no.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.feature_flags import trajectory_sample_rate

if TYPE_CHECKING:
    from robothor.engine.models import AgentRun
    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)


_ROLE_TO_SHAREGPT_FROM = {
    "system": "system",
    "user": "human",
    "assistant": "gpt",
    "tool": "tool",
}


def _to_sharegpt(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert Genus's openai-shaped message list to ShareGPT entries.

    Genus messages carry ``role`` / ``content`` (+ optional
    ``tool_calls``); ShareGPT wants ``{from, value}``. Tool messages
    are emitted with ``from='tool'``. Multi-modal / list-typed
    ``content`` is flattened to a string so the JSONL stays uniform.
    """
    out: list[dict[str, str]] = []
    for msg in messages:
        role = str(msg.get("role", "") or "")
        speaker = _ROLE_TO_SHAREGPT_FROM.get(role, role or "unknown")
        content = msg.get("content")
        if content is None and msg.get("tool_calls"):
            # Pure tool-call assistant message — serialise the calls.
            content = json.dumps({"tool_calls": msg["tool_calls"]}, default=str)
        if isinstance(content, list):
            content = " ".join(
                str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content
            )
        if content is None:
            content = ""
        out.append({"from": speaker, "value": str(content)})
    return out


def _output_path(tenant_id: str, completed: bool, *, base: Path | None = None) -> Path:
    """Resolve the JSONL path for a run snapshot."""
    workspace = base or Path(__file__).resolve().parent.parent.parent / "workspace"
    when = datetime.now(UTC).strftime("%Y-%m")
    filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"
    return workspace / "trajectories" / (tenant_id or DEFAULT_TENANT) / when / filename


def save_trajectory(
    session: AgentSession,
    *,
    completed: bool,
    base: Path | None = None,
    sample_rate_override: float | None = None,
) -> Path | None:
    """Persist ``session.messages`` as one JSONL line.

    Returns the file written, or ``None`` when the sample dice say no
    or the run produced no messages (nothing to capture). Always
    safe to call from sync code in the hot path — every disk
    operation is wrapped in best-effort exception handling and
    failures log-and-return-None.
    """
    rate = trajectory_sample_rate() if sample_rate_override is None else sample_rate_override
    if rate <= 0.0:
        return None
    if rate < 1.0 and random.random() > rate:
        return None
    if not session.messages:
        return None

    tenant_id = getattr(session.run, "tenant_id", "") or DEFAULT_TENANT
    path = _output_path(tenant_id, completed, base=base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trajectory: failed to mkdir %s: %s", path.parent, exc)
        return None

    record = {
        "run_id": session.run_id,
        "agent_id": getattr(session.run, "agent_id", ""),
        "tenant_id": tenant_id,
        "completed": completed,
        "captured_at": datetime.now(UTC).isoformat(),
        "messages": _to_sharegpt(session.messages),
    }

    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("trajectory: failed to write %s: %s", path, exc)
        return None

    logger.debug("trajectory: wrote run %s to %s", session.run_id, path)
    return path


def save_trajectory_for_run(
    session: AgentSession,
    run: AgentRun,
    *,
    base: Path | None = None,
) -> Path | None:
    """Convenience wrapper used by the runner's post-response hook.

    Classifies the run as completed-vs-failed off the AgentRun
    status enum so callers don't have to import RunStatus.
    """
    completed = getattr(run, "status", None) and str(run.status).lower().endswith("completed")
    return save_trajectory(session, completed=bool(completed), base=base)
