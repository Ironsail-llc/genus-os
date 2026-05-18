"""Codex subscription-backed provider helpers.

This module deliberately uses the local Codex CLI session instead of an
OpenAI API key. That keeps billing on the user's ChatGPT/Codex entitlement
when Codex is logged in with ChatGPT.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class CodexProviderError(RuntimeError):
    """Base class for Codex provider failures."""


def is_codex_model(model: str) -> bool:
    """Return True when *model* should route through local Codex auth."""
    return model.startswith("codex/")


def codex_model_name(model: str) -> str:
    """Map Genus model ids to Codex CLI model ids."""
    return model.removeprefix("codex/")


def _codex_binary() -> str:
    binary = os.environ.get("ROBOTHOR_CODEX_BIN", "codex")
    resolved = shutil.which(binary)
    if not resolved:
        raise CodexProviderError(f"Codex CLI not found: {binary}")
    return resolved


def _workspace() -> Path:
    return Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.cwd())).expanduser()


def _codex_home() -> str | None:
    value = os.environ.get("ROBOTHOR_CODEX_HOME") or os.environ.get("CODEX_HOME")
    return str(Path(value).expanduser()) if value else None


def _codex_env() -> dict[str, str]:
    """Build a subprocess environment that cannot silently switch to API billing."""
    env = dict(os.environ)
    # If these are inherited, Codex can authenticate with usage-based billing.
    # Subscription-backed Genus calls must use the cached ChatGPT login instead.
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT",
    ):
        env.pop(key, None)
    home = _codex_home()
    if home:
        env["CODEX_HOME"] = home
    return env


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = [
        "You are serving as the remote language model for Genus OS.",
        "Return only the assistant response for the final user turn.",
        "Do not modify files, run commands, or describe this wrapper.",
        "",
        "Conversation:",
    ]
    for msg in messages:
        role = str(msg.get("role", "user")).upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            rendered = []
            for item in content:
                if isinstance(item, dict):
                    rendered.append(str(item.get("text") or item.get("content") or item))
                else:
                    rendered.append(str(item))
            content = "\n".join(rendered)
        parts.append(f"\n[{role}]\n{content}")
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            parts.append(f"Tool calls requested by assistant:\n{json.dumps(tool_calls)}")
        if role == "TOOL":
            tool_name = msg.get("name") or ""
            tool_call_id = msg.get("tool_call_id") or ""
            parts.append(f"Tool result metadata: name={tool_name} tool_call_id={tool_call_id}")
    return "\n".join(parts).strip()


def _tool_selection_schema() -> dict[str, Any]:
    """Strict JSON schema accepted by Codex/OpenAI structured outputs."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "content", "tool_calls"],
        "properties": {
            "type": {"type": "string", "enum": ["final", "tool_calls"]},
            "content": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "arguments_json"],
                    "properties": {
                        "name": {"type": "string"},
                        "arguments_json": {"type": "string"},
                    },
                },
            },
        },
    }


def _tool_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    """Render a host-tool-selection prompt for Codex."""
    available: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        available.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or fn.get("input_schema") or {},
            }
        )

    return (
        "You are serving as the remote language model for Genus OS.\n"
        "You are not executing tools yourself. You are selecting tool calls for the "
        "Genus host application to execute.\n"
        "If a tool should be called, return type=tool_calls, empty content, and one "
        "or more tool_calls. Each tool call must use an available tool name and "
        "arguments_json must be compact JSON matching that tool's parameters.\n"
        "If no tool is needed, return type=final with the assistant response in content "
        "and an empty tool_calls array.\n\n"
        f"Available tools:\n{json.dumps(available, ensure_ascii=False)}\n\n"
        f"{_messages_to_prompt(messages)}"
    )


def _parse_tool_selection(raw: str, model: str) -> Any:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CodexProviderError(f"Codex returned invalid tool-selection JSON: {e}") from e

    tool_calls = []
    for idx, call in enumerate(payload.get("tool_calls") or []):
        name = str(call.get("name") or "")
        arguments_json = str(call.get("arguments_json") or "{}")
        try:
            json.loads(arguments_json)
        except json.JSONDecodeError as e:
            raise CodexProviderError(
                f"Codex returned invalid arguments_json for {name}: {e}"
            ) from e
        tool_calls.append(
            SimpleNamespace(
                id=f"codex_tool_{idx + 1}",
                type="function",
                function=SimpleNamespace(name=name, arguments=arguments_json),
            )
        )

    content = str(payload.get("content") or "")
    if payload.get("type") == "tool_calls" and not tool_calls:
        raise CodexProviderError("Codex selected tool_calls but returned no tool calls")

    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls" if tool_calls else "stop",
                message=SimpleNamespace(content=content or None, tool_calls=tool_calls or None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )


async def login_status(timeout: float = 15) -> str:
    """Return `codex login status` output using the configured Codex home."""
    proc = await asyncio.create_subprocess_exec(
        _codex_binary(),
        "login",
        "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_codex_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    output = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode != 0:
        raise CodexProviderError(output or "codex login status failed")
    return output


async def ensure_chatgpt_login() -> None:
    """Fail unless Codex is currently authenticated through ChatGPT."""
    status = await login_status()
    if "ChatGPT" not in status:
        raise CodexProviderError(
            "Codex is not logged in with ChatGPT subscription auth. "
            "Run `robothor codex login` as the Genus service user."
        )


async def _run_codex_exec(
    *,
    model: str,
    prompt: str,
    timeout: float,
    output_schema: dict[str, Any] | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="robothor-codex-") as tmpdir:
        output_path = Path(tmpdir) / "last-message.txt"
        cmd = [
            _codex_binary(),
            "exec",
            "--model",
            codex_model_name(model),
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_path),
            "--cd",
            str(_workspace()),
        ]
        if output_schema is not None:
            schema_path = Path(tmpdir) / "output-schema.json"
            schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
            cmd.extend(["--output-schema", str(schema_path)])
        cmd.append("-")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_codex_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()),
            timeout=timeout,
        )
        if proc.returncode != 0:
            detail = (stderr or stdout).decode(errors="replace").strip()
            raise CodexProviderError(detail or f"codex exec failed with exit {proc.returncode}")
        return output_path.read_text(encoding="utf-8").strip()


async def acompletion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    timeout: float = 300,
    **_: Any,
) -> Any:
    """Return a litellm-shaped response using local Codex subscription auth.

    Tool-call turns are adapted through structured output: Codex selects host
    tool calls, then the existing Genus engine executes those tools.
    """
    await ensure_chatgpt_login()
    if tools:
        raw = await _run_codex_exec(
            model=model,
            prompt=_tool_prompt(messages, tools),
            timeout=timeout,
            output_schema=_tool_selection_schema(),
        )
        return _parse_tool_selection(raw, model)

    content = await _run_codex_exec(
        model=model,
        prompt=_messages_to_prompt(messages),
        timeout=timeout,
    )

    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )
