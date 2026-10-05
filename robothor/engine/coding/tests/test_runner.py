"""The `claude -p` invocation: argv, the stream parser, and the subprocess."""

from __future__ import annotations

import json
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from robothor.engine.coding.runner import (
    ClaudeInvocation,
    StreamParser,
    build_argv,
    check_mode,
    run_claude,
)
from robothor.engine.coding.tests.conftest import FIXTURES

if TYPE_CHECKING:
    from pathlib import Path


def _inv(tmp_path: Path, **kw) -> ClaudeInvocation:
    return ClaudeInvocation(prompt="do it", cwd=tmp_path, binary="/usr/bin/claude", **kw)


# ── argv ──────────────────────────────────────────────────────────────


def test_argv_is_headless_stream_json_and_isolated_from_personal_config(tmp_path):
    argv = build_argv(_inv(tmp_path))

    assert argv[:2] == ["/usr/bin/claude", "-p"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    # The operator's user/project/local settings and MCP servers never load.
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    # Headless: nothing may sit waiting on a permission prompt.
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    # The prompt goes over stdin, never argv (argv is world-readable in /proc).
    assert "do it" not in argv
    assert "--resume" not in argv


def test_argv_carries_every_optional_flag(tmp_path):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    argv = build_argv(
        _inv(
            tmp_path,
            model="sonnet",
            effort="high",
            allowed_tools=("Read", "Bash(git diff:*)"),
            disallowed_tools=("Write",),
            max_turns=12,
            max_budget_usd=1.5,
            append_system_prompt="be brief",
            json_schema=schema,
            resume_session_id="5f0c7a52-0000-4000-8000-000000000001",
        )
    )

    def val(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    assert val("--model") == "sonnet"
    assert val("--effort") == "high"
    assert val("--allowedTools") == "Read,Bash(git diff:*)"
    assert val("--disallowedTools") == "Write"
    assert val("--max-turns") == "12"
    assert val("--max-budget-usd") == "1.50"
    assert val("--append-system-prompt") == "be brief"
    assert json.loads(val("--json-schema")) == schema
    assert val("--resume") == "5f0c7a52-0000-4000-8000-000000000001"


def test_argv_without_effort_passes_none(tmp_path):
    assert "--effort" not in build_argv(_inv(tmp_path))


def test_argv_refuses_an_effort_the_cli_does_not_know(tmp_path):
    with pytest.raises(ValueError, match="effort"):
        build_argv(_inv(tmp_path, effort="--dangerously-skip-permissions"))


def test_argv_refuses_a_resume_id_that_is_not_a_session_uuid(tmp_path):
    with pytest.raises(ValueError):
        build_argv(_inv(tmp_path, resume_session_id="--dangerously-skip-permissions"))


def test_modes_are_closed():
    assert check_mode("code") == "code"
    with pytest.raises(ValueError):
        check_mode("yolo")


def test_argv_carries_the_sandbox_settings_as_json(tmp_path):
    settings = {"sandbox": {"enabled": True, "failIfUnavailable": True}}
    argv = build_argv(_inv(tmp_path, settings=settings))
    assert json.loads(argv[argv.index("--settings") + 1]) == settings
    # Explicit settings survive --setting-sources "" (which drops only files).
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_argv_without_settings_passes_none(tmp_path):
    assert "--settings" not in build_argv(_inv(tmp_path))


def test_a_call_with_no_result_line_says_its_cost_is_unknown():
    parser = StreamParser()
    parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
    result = parser.finish(exit_code=None, stderr_tail="", timed_out=True)
    assert result.got_result is False


# ── stream parser ─────────────────────────────────────────────────────


def _parse(name: str) -> tuple[StreamParser, list]:
    parser = StreamParser()
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        events.extend(parser.feed(line))
    return parser, events


def test_parser_reads_a_real_captured_stream():
    parser, events = _parse("stream_say_ok.jsonl")
    result = parser.finish(exit_code=0, stderr_tail="", timed_out=False)

    assert result.session_id == "92fdd60b-1a9f-463e-987e-afd221653f2b"
    assert result.is_error is False
    assert result.subtype == "success"
    assert result.result_text == "ok"
    assert result.num_turns == 1
    assert result.total_cost_usd == pytest.approx(0.0162776)
    assert [e.kind for e in events][0] == "init"
    assert any(e.kind == "text" and e.summary == "ok" for e in events)


def test_parser_summarises_tool_use_and_structured_output():
    parser, events = _parse("stream_tool_use.jsonl")
    result = parser.finish(exit_code=0, stderr_tail="", timed_out=False)

    tool_events = [e for e in events if e.kind == "tool_use"]
    assert tool_events[0].summary.startswith("Bash: pytest -q")
    assert tool_events[1].summary.startswith("Edit: /tmp/repo/calc.py")
    assert any(e.kind == "tool_result" and e.summary.startswith("error:") for e in events)
    assert result.structured_output == {"verdict": "fixed"}
    assert result.num_turns == 3
    assert result.total_cost_usd == pytest.approx(0.0421)


def test_parser_survives_garbage_and_reports_a_missing_result():
    parser = StreamParser()
    assert parser.feed("not json") == []
    assert parser.feed("") == []
    parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
    result = parser.finish(exit_code=1, stderr_tail="boom", timed_out=False)

    assert result.is_error is True
    assert result.session_id == "s-1"
    assert "boom" in result.error_summary


# ── the subprocess ────────────────────────────────────────────────────


def _fake_claude(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


async def test_run_claude_streams_the_fixture_and_feeds_the_prompt_on_stdin(tmp_path):
    fixture = FIXTURES / "stream_tool_use.jsonl"
    binary = _fake_claude(
        tmp_path,
        f"""
        import sys, os, pathlib
        pathlib.Path("stdin.txt").write_text(sys.stdin.read())
        pathlib.Path("argv.txt").write_text("\\n".join(sys.argv[1:]))
        sys.stdout.write(open({str(fixture)!r}).read())
        """,
    )
    seen = []
    inv = ClaudeInvocation(prompt="fix the bug", cwd=tmp_path, binary=binary)

    result = await run_claude(
        inv, env={"PATH": "/usr/bin:/bin"}, timeout_s=30, on_event=seen.append
    )

    assert result.is_error is False
    assert result.exit_code == 0
    assert result.session_id == "5f0c7a52-0000-4000-8000-000000000001"
    assert (tmp_path / "stdin.txt").read_text() == "fix the bug"
    assert "fix the bug" not in (tmp_path / "argv.txt").read_text()
    assert [e.kind for e in seen].count("tool_use") == 2


async def test_run_claude_times_out_and_kills_the_process_group(tmp_path):
    marker = tmp_path / "child-alive"
    binary = _fake_claude(
        tmp_path,
        f"""
        import subprocess, sys, time, json
        print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s-timeout"}}), flush=True)
        # A grandchild in the same process group must die with it.
        subprocess.Popen([sys.executable, "-c",
            "import time,pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"])
        time.sleep(60)
        """,
    )
    inv = ClaudeInvocation(prompt="x", cwd=tmp_path, binary=binary)

    result = await run_claude(inv, env={"PATH": "/usr/bin:/bin"}, timeout_s=1)

    assert result.timed_out is True
    assert result.is_error is True
    assert result.session_id == "s-timeout"
    import asyncio

    await asyncio.sleep(3.5)
    assert not marker.exists(), "the grandchild outlived the timeout kill"
