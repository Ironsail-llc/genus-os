"""Tests for Docker sandbox isolation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.sandbox import (
    Sandbox,
    SandboxMode,
    _used_ports,
    get_current_sandbox,
    set_current_sandbox,
)


class TestSandboxLocal:
    """Tests for local (passthrough) sandbox mode."""

    def test_local_mode_defaults(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        assert s.display == ":99"
        assert s.container_id is None
        assert s.cdp_port is None

    @pytest.mark.asyncio
    async def test_local_start_is_noop(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        await s.start()
        assert s._started is True
        assert s.container_id is None

    @pytest.mark.asyncio
    async def test_local_stop_is_noop(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        await s.start()
        await s.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_local_exec_runs_subprocess(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        await s.start()
        result = await s.exec(["echo", "hello"])
        assert result["stdout"] == "hello"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_local_exec_captures_errors(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        await s.start()
        result = await s.exec(["false"])
        assert result.get("exit_code") != 0 or "error" in result

    @pytest.mark.asyncio
    async def test_local_copy_from_is_noop(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        assert await s.copy_from("/tmp/x", "/tmp/y") is True

    def test_local_browser_endpoint_empty(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        assert s.browser_endpoint() == ""


class TestSandboxDocker:
    """Tests for Docker sandbox mode (mocked Docker commands)."""

    @pytest.mark.asyncio
    async def test_docker_start(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="test-run-123")
        with patch("robothor.engine.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123container\n", stderr="")
            await s.start()
        assert s.container_id == "abc123container"
        assert s.display == ":0"
        assert s.cdp_port is not None
        assert s._started is True
        # Cleanup
        _used_ports.discard(s.cdp_port)

    @pytest.mark.asyncio
    async def test_docker_start_failure(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="fail-run")
        with patch("robothor.engine.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no docker")
            with pytest.raises(RuntimeError, match="Docker sandbox start failed"):
                await s.start()

    @pytest.mark.asyncio
    async def test_docker_exec(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="exec-run")
        s.container_id = "abc123"
        s._started = True
        with patch("robothor.engine.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="output\n", stderr="")
            result = await s.exec(["xdotool", "getactivewindow"])
        assert result["stdout"] == "output"
        mock_run.assert_called_once()
        # Verify docker exec was used
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "docker"
        assert call_args[1] == "exec"

    @pytest.mark.asyncio
    async def test_docker_exec_no_container(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="no-container")
        result = await s.exec(["ls"])
        assert "error" in result

    @pytest.mark.asyncio
    async def test_docker_stop(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="stop-run")
        s.container_id = "abc123"
        s.cdp_port = 19250
        _used_ports.add(19250)
        with patch("robothor.engine.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await s.stop()
        assert s.container_id is None
        assert 19250 not in _used_ports

    @pytest.mark.asyncio
    async def test_docker_copy_from(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="copy-run")
        s.container_id = "abc123"
        with patch("robothor.engine.sandbox.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            ok = await s.copy_from("/tmp/screenshot.png", "/host/path.png")
        assert ok is True

    def test_docker_browser_endpoint(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="browser-run")
        s.cdp_port = 19222
        assert s.browser_endpoint() == "http://localhost:19222"

    def test_port_allocation(self):
        s = Sandbox(mode=SandboxMode.DOCKER, run_id="port-test")
        port = s._allocate_port()
        assert port >= 19222
        assert port in _used_ports
        _used_ports.discard(port)

    def test_port_allocation_unique(self):
        ports = set()
        sandboxes = []
        for i in range(5):
            s = Sandbox(mode=SandboxMode.DOCKER, run_id=f"port-{i}")
            port = s._allocate_port()
            assert port not in ports
            ports.add(port)
            sandboxes.append(s)
        # Cleanup
        for p in ports:
            _used_ports.discard(p)


class TestSandboxContextVar:
    def test_set_and_get(self):
        s = Sandbox(mode=SandboxMode.LOCAL)
        set_current_sandbox(s)
        assert get_current_sandbox() is s
        set_current_sandbox(None)
        assert get_current_sandbox() is None

    def test_default_is_none(self):
        set_current_sandbox(None)
        assert get_current_sandbox() is None
