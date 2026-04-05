"""Tests for desktop control tools, browser automation, and desktop_safety guardrail."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.tools.dispatch import ToolContext

# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def ctx():
    return ToolContext(
        agent_id="computer-use", tenant_id="robothor-primary", workspace=str(Path.cwd())
    )


@pytest.fixture
def mock_cfg():
    cfg = MagicMock()
    cfg.desktop_display = ":99"
    cfg.ollama.host = "127.0.0.1"
    cfg.ollama.port = 11434
    cfg.ollama.vision_model = "llama3.2-vision:11b"
    with patch("robothor.engine.tools.dispatch._cfg", return_value=cfg):
        yield cfg


# ─── Guardrail: desktop_safety ───────────────────────────────────────


class TestDesktopSafetyGuardrail:
    def test_blocks_bash_launch(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_launch", {"app": "bash"})
        assert not result.allowed
        assert "terminal emulator" in result.reason

    def test_blocks_xterm_launch(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_launch", {"app": "xterm"})
        assert not result.allowed

    def test_blocks_gnome_terminal_launch(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_launch", {"app": "gnome-terminal"})
        assert not result.allowed

    def test_blocks_absolute_path_terminal(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_launch", {"app": "/usr/bin/bash"})
        assert not result.allowed

    def test_allows_firefox_launch(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_launch", {"app": "firefox"})
        assert result.allowed

    def test_allows_libreoffice_launch(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_launch", {"app": "libreoffice"})
        assert result.allowed

    def test_blocks_ctrl_alt_delete(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_key", {"key": "ctrl+alt+delete"})
        assert not result.allowed

    def test_blocks_ctrl_alt_f1(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_key", {"key": "ctrl+alt+f1"})
        assert not result.allowed

    def test_allows_ctrl_a(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_key", {"key": "ctrl+a"})
        assert result.allowed

    def test_allows_alt_f4(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("desktop_key", {"key": "alt+F4"})
        assert result.allowed

    def test_blocks_file_url(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution(
            "browser", {"action": "navigate", "url": "file:///etc/passwd"}
        )
        assert not result.allowed

    def test_blocks_javascript_url(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution(
            "browser", {"action": "navigate", "url": "javascript:alert(1)"}
        )
        assert not result.allowed

    def test_allows_https_url(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution(
            "browser", {"action": "navigate", "url": "https://example.com"}
        )
        assert result.allowed

    def test_allows_browser_start(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("browser", {"action": "start"})
        assert result.allowed

    def test_allows_non_desktop_tools(self):
        engine = GuardrailEngine(enabled_policies=["desktop_safety"])
        result = engine.check_pre_execution("read_file", {"path": "/tmp/x"})
        assert result.allowed


# ─── Desktop tool coordinate validation ──────────────────────────────


class TestCoordinateValidation:
    def test_valid_coordinates(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords(100, 200)
        assert result == (100, 200)

    def test_negative_x(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords(-1, 200)
        assert isinstance(result, dict) and "error" in result

    def test_negative_y(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords(100, -1)
        assert isinstance(result, dict) and "error" in result

    def test_out_of_bounds(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords(1500, 200)
        assert isinstance(result, dict) and "error" in result

    def test_non_numeric(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords("abc", 200)
        assert isinstance(result, dict) and "error" in result

    def test_zero_coordinates(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords(0, 0)
        assert result == (0, 0)

    def test_max_coordinates(self):
        from robothor.engine.tools.handlers.desktop import _validate_coords

        result = _validate_coords(1280, 1024)
        assert result == (1280, 1024)


# ─── Desktop screenshot tool ─────────────────────────────────────────


class TestDesktopScreenshot:
    @pytest.mark.asyncio
    async def test_screenshot_calls_scrot(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _screenshot

        with (
            patch("robothor.engine.tools.handlers.desktop.subprocess") as mock_sub,
            patch("robothor.engine.tools.handlers.desktop.os.unlink"),
        ):
            # Mock scrot failure to avoid file I/O
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="scrot not available"
            )
            result = await _screenshot({}, ctx)

            # Verify scrot was called
            assert mock_sub.run.call_count >= 1
            first_call = mock_sub.run.call_args_list[0]
            assert "scrot" in first_call[0][0]
            # Should return error since scrot "failed"
            assert "error" in result

    @pytest.mark.asyncio
    async def test_screenshot_includes_display_env(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _env

        env = _env()
        assert env["DISPLAY"] == ":99"


# ─── Desktop click tool ──────────────────────────────────────────────


class TestDesktopClick:
    @pytest.mark.asyncio
    async def test_click_calls_xdotool(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _click

        with patch("robothor.engine.tools.handlers.desktop._run_xdotool") as mock_xdo:
            mock_xdo.return_value = {"stdout": "", "exit_code": 0}
            await _click({"x": 100, "y": 200}, ctx)
            mock_xdo.assert_called_once_with("mousemove", "100", "200", "click", "1")

    @pytest.mark.asyncio
    async def test_click_rejects_invalid_coords(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _click

        result = await _click({"x": -1, "y": 200}, ctx)
        assert "error" in result


# ─── Desktop type tool ───────────────────────────────────────────────


class TestDesktopType:
    @pytest.mark.asyncio
    async def test_type_calls_xdotool(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _type_text

        with patch("robothor.engine.tools.handlers.desktop._run_xdotool") as mock_xdo:
            mock_xdo.return_value = {"stdout": "", "exit_code": 0}
            await _type_text({"text": "hello world"}, ctx)
            mock_xdo.assert_called_once_with("type", "--delay", "50", "--", "hello world")

    @pytest.mark.asyncio
    async def test_type_rejects_empty_text(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _type_text

        result = await _type_text({"text": ""}, ctx)
        assert "error" in result


# ─── Desktop key tool ────────────────────────────────────────────────


class TestDesktopKey:
    @pytest.mark.asyncio
    async def test_key_calls_xdotool(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _key

        with patch("robothor.engine.tools.handlers.desktop._run_xdotool") as mock_xdo:
            mock_xdo.return_value = {"stdout": "", "exit_code": 0}
            await _key({"key": "ctrl+a"}, ctx)
            mock_xdo.assert_called_once_with("key", "--", "ctrl+a")

    @pytest.mark.asyncio
    async def test_key_rejects_empty(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _key

        result = await _key({"key": ""}, ctx)
        assert "error" in result


# ─── Desktop scroll tool ─────────────────────────────────────────────


class TestDesktopScroll:
    @pytest.mark.asyncio
    async def test_scroll_down(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _scroll

        with patch("robothor.engine.tools.handlers.desktop._run_xdotool") as mock_xdo:
            mock_xdo.return_value = {"stdout": "", "exit_code": 0}
            await _scroll({"direction": "down", "clicks": 5}, ctx)
            mock_xdo.assert_called_once_with("click", "--repeat", "5", "5")

    @pytest.mark.asyncio
    async def test_scroll_up(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _scroll

        with patch("robothor.engine.tools.handlers.desktop._run_xdotool") as mock_xdo:
            mock_xdo.return_value = {"stdout": "", "exit_code": 0}
            await _scroll({"direction": "up", "clicks": 3}, ctx)
            mock_xdo.assert_called_once_with("click", "--repeat", "3", "4")

    @pytest.mark.asyncio
    async def test_scroll_caps_at_20(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _scroll

        with patch("robothor.engine.tools.handlers.desktop._run_xdotool") as mock_xdo:
            mock_xdo.return_value = {"stdout": "", "exit_code": 0}
            await _scroll({"direction": "down", "clicks": 100}, ctx)
            mock_xdo.assert_called_once_with("click", "--repeat", "20", "5")


# ─── Desktop window list tool ────────────────────────────────────────


class TestDesktopWindowList:
    @pytest.mark.asyncio
    async def test_parses_wmctrl_output(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _window_list

        wmctrl_output = "0x01400003  0 0    0    1280 1024 hostname Desktop\n0x02400005  0 100  50   800  600  hostname Firefox"

        with patch("robothor.engine.tools.handlers.desktop.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=wmctrl_output, stderr="")
            result = await _window_list({}, ctx)
            assert result["count"] == 2
            assert result["windows"][1]["title"] == "Firefox"
            assert result["windows"][1]["width"] == 800


# ─── Desktop launch tool ─────────────────────────────────────────────


class TestDesktopLaunch:
    @pytest.mark.asyncio
    async def test_launch_rejects_empty_app(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _launch

        result = await _launch({"app": ""}, ctx)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_launch_calls_popen(self, ctx, mock_cfg):
        from robothor.engine.tools.handlers.desktop import _launch

        with (
            patch("robothor.engine.tools.handlers.desktop.subprocess") as mock_sub,
            patch("robothor.engine.tools.handlers.desktop.time"),
        ):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 12345
            mock_sub.Popen.return_value = mock_proc
            mock_sub.DEVNULL = -1

            result = await _launch({"app": "firefox"}, ctx)
            assert result["launched"] == "firefox"
            assert result["pid"] == 12345


# ─── Desktop tool schemas ────────────────────────────────────────────


class TestDesktopToolSchemas:
    def test_desktop_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for tool_name in [
                "desktop_screenshot",
                "desktop_click",
                "desktop_double_click",
                "desktop_right_click",
                "desktop_mouse_move",
                "desktop_drag",
                "desktop_scroll",
                "desktop_type",
                "desktop_key",
                "desktop_window_list",
                "desktop_window_focus",
                "desktop_launch",
                "desktop_describe",
            ]:
                assert tool_name in registry._schemas, f"Missing schema for {tool_name}"

    def test_browser_tool_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            assert "browser" in registry._schemas


# ─── Desktop tool constants ──────────────────────────────────────────


class TestDesktopConstants:
    def test_desktop_tools_frozenset(self):
        from robothor.engine.tools.constants import DESKTOP_TOOLS

        assert "desktop_screenshot" in DESKTOP_TOOLS
        assert "desktop_click" in DESKTOP_TOOLS
        assert len(DESKTOP_TOOLS) == 13

    def test_browser_tools_frozenset(self):
        from robothor.engine.tools.constants import BROWSER_TOOLS

        assert "browser" in BROWSER_TOOLS

    def test_readonly_tools_include_desktop(self):
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert "desktop_screenshot" in READONLY_TOOLS
        assert "desktop_window_list" in READONLY_TOOLS
        assert "desktop_describe" in READONLY_TOOLS
        # These should NOT be read-only
        assert "desktop_click" not in READONLY_TOOLS
        assert "desktop_type" not in READONLY_TOOLS
        assert "desktop_launch" not in READONLY_TOOLS


# ─── Audit trail ─────────────────────────────────────────────────────


class TestDesktopAudit:
    def test_log_action(self, tmp_path):
        from robothor.engine.tools import desktop_audit

        # Override paths for testing
        desktop_audit.AUDIT_LOG = tmp_path / "audit.jsonl"

        desktop_audit.log_action(
            action="click",
            tool_name="desktop_click",
            agent_id="test-agent",
            run_id="run-123",
            args={"x": 100, "y": 200},
        )

        import json

        lines = desktop_audit.AUDIT_LOG.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "click"
        assert entry["agent_id"] == "test-agent"
        assert entry["args"]["x"] == 100

    def test_log_action_excludes_base64(self, tmp_path):
        from robothor.engine.tools import desktop_audit

        desktop_audit.AUDIT_LOG = tmp_path / "audit.jsonl"

        desktop_audit.log_action(
            action="screenshot",
            tool_name="desktop_screenshot",
            args={"screenshot_base64": "very_long_data"},
        )

        import json

        entry = json.loads(desktop_audit.AUDIT_LOG.read_text().strip())
        assert "screenshot_base64" not in entry.get("args", {})

    def test_save_screenshot(self, tmp_path):
        from robothor.engine.tools import desktop_audit

        desktop_audit.AUDIT_DIR = tmp_path / "screenshots"

        path = desktop_audit.save_screenshot(
            b"\x89PNG\r\n\x1a\n",
            run_id="run-456",
            agent_id="test",
        )
        assert path != ""
        assert "run-456" in path
        from pathlib import Path

        assert Path(path).exists()
