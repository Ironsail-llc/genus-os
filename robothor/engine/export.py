"""Session export — structured markdown for agent and chat sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.session import AgentSession


def agent_session_to_markdown(session: AgentSession) -> str:
    """Export an AgentSession as structured markdown."""
    run = session.run
    lines: list[str] = []

    lines.append(f"# Agent Run: {run.agent_id}")
    lines.append("")
    lines.append(f"- **Run ID:** `{run.id}`")
    lines.append(f"- **Status:** {run.status.value}")
    lines.append(f"- **Trigger:** {run.trigger_type.value}")
    if run.started_at:
        lines.append(f"- **Started:** {run.started_at.isoformat()}")
    if run.completed_at:
        lines.append(f"- **Completed:** {run.completed_at.isoformat()}")
    if run.duration_ms is not None:
        lines.append(f"- **Duration:** {run.duration_ms}ms")
    lines.append(f"- **Model:** {run.model_used or 'N/A'}")
    lines.append("")

    # Message transcript
    lines.append("## Transcript")
    lines.append("")
    for msg in session.messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "system":
            lines.append("<details><summary>System Prompt</summary>")
            lines.append("")
            lines.append(f"```\n{_truncate(content, 2000)}\n```")
            lines.append("</details>")
            lines.append("")
        elif role == "user":
            lines.append(f"**User:** {_truncate(content, 1000)}")
            lines.append("")
        elif role == "assistant":
            if content:
                lines.append(f"**Assistant:** {_truncate(content, 2000)}")
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    lines.append(
                        f"  - Tool call: `{fn.get('name', '?')}"
                        f"({_truncate(fn.get('arguments', ''), 200)})`"
                    )
            lines.append("")
        elif role == "tool":
            tid = msg.get("tool_call_id", "")
            lines.append(f"**Tool Result** (`{tid}`): {_truncate(content, 500)}")
            lines.append("")

    # Cost summary
    lines.append("## Cost Summary")
    lines.append("")
    lines.append(f"- **Input tokens:** {run.input_tokens:,}")
    lines.append(f"- **Output tokens:** {run.output_tokens:,}")
    if run.cache_read_tokens:
        lines.append(f"- **Cache read tokens:** {run.cache_read_tokens:,}")
    if run.cache_creation_tokens:
        lines.append(f"- **Cache creation tokens:** {run.cache_creation_tokens:,}")
    lines.append(f"- **Total cost:** ${run.total_cost_usd:.4f}")

    if run.error_message:
        lines.append("")
        lines.append("## Error")
        lines.append(f"```\n{run.error_message}\n```")

    return "\n".join(lines)


def chat_session_to_markdown(
    session: Any,
    session_key: str = "",
) -> str:
    """Export a ChatSession as structured markdown."""
    lines: list[str] = []

    lines.append("# Chat Session Export")
    lines.append("")
    lines.append(f"- **Session:** `{session_key}`")
    lines.append(f"- **Exported:** {datetime.now(UTC).isoformat()}")
    lines.append(f"- **Messages:** {len(session.history)}")
    if session.model_override:
        lines.append(f"- **Model Override:** {session.model_override}")
    lines.append("")

    lines.append("## Conversation")
    lines.append("")
    for msg in session.history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append("### User")
            lines.append(content)
            lines.append("")
        elif role == "assistant":
            lines.append("### Assistant")
            lines.append(content)
            lines.append("")

    return "\n".join(lines)


def _truncate(text: str | None, max_len: int) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
