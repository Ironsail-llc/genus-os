"""Gateway process lifecycle — start, stop, restart, health check."""

from __future__ import annotations

import os
import signal
import subprocess
import textwrap
from pathlib import Path

import httpx


class GatewayProcess:
    """Manage the OpenClaw gateway Node.js process."""

    def __init__(
        self,
        gateway_dir: Path | None = None,
        config_dir: Path | None = None,
        port: int = 18789,
        pidfile: Path | None = None,
    ):
        self.gateway_dir = gateway_dir or Path(__file__).parents[2] / "gateway"
        self.config_dir = config_dir or Path(
            os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")
        )
        self.port = port
        self.pidfile = pidfile or Path("/tmp/robothor-gateway.pid")

    def start(self, foreground: bool = False) -> int:
        """Start the gateway process. Returns PID (0 if foreground mode)."""
        entry = self.gateway_dir / "dist" / "index.js"
        if not entry.exists():
            raise FileNotFoundError(
                f"Gateway not built: {entry} missing. Run 'robothor gateway build' first."
            )

        env = os.environ.copy()
        env["OPENCLAW_HOME"] = str(self.config_dir)

        cmd = ["node", str(entry), "gateway"]

        if foreground:
            proc = subprocess.run(cmd, env=env, cwd=str(self.gateway_dir))
            return 0

        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(self.gateway_dir),
            stdout=open("/tmp/robothor-gateway.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.pidfile.write_text(str(proc.pid))
        return proc.pid

    def stop(self) -> bool:
        """Stop the gateway process. Returns True if stopped."""
        pid = self._read_pid()
        if pid is None:
            return False

        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for graceful shutdown
            import time

            for _ in range(10):
                try:
                    os.kill(pid, 0)  # Check if still alive
                    time.sleep(0.5)
                except OSError:
                    break
            self.pidfile.unlink(missing_ok=True)
            return True
        except OSError:
            self.pidfile.unlink(missing_ok=True)
            return False

    def restart(self) -> bool:
        """Stop then start the gateway."""
        self.stop()
        return self.start() > 0

    def is_running(self) -> bool:
        """Check if the gateway process is alive."""
        pid = self._read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def health_check(self) -> dict:
        """Check gateway health via HTTP."""
        try:
            resp = httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=5)
            return {"healthy": resp.status_code == 200, "status_code": resp.status_code}
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    def generate_systemd_unit(self, user: str = "philip") -> str:
        """Generate a systemd service unit file for the gateway."""
        entry = self.gateway_dir / "dist" / "index.js"
        secrets_script = Path.home() / "robothor" / "scripts" / "decrypt-secrets.sh"

        return textwrap.dedent(f"""\
            [Unit]
            Description=Robothor Gateway (OpenClaw)
            After=network-online.target postgresql.service redis.service
            Wants=network-online.target

            [Service]
            Type=simple
            User={user}
            WorkingDirectory={self.gateway_dir}
            Environment=OPENCLAW_HOME={self.config_dir}
            ExecStartPre={secrets_script}
            EnvironmentFile=/run/robothor/secrets.env
            ExecStart=/usr/bin/node {entry} gateway
            Restart=always
            RestartSec=5
            KillMode=control-group

            [Install]
            WantedBy=multi-user.target
        """)

    def install_systemd_unit(self) -> Path:
        """Write systemd unit file. Requires sudo for enable."""
        unit_content = self.generate_systemd_unit()
        unit_path = Path("/etc/systemd/system/robothor-gateway.service")

        # Write via sudo
        proc = subprocess.run(
            ["sudo", "tee", str(unit_path)],
            input=unit_content,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to write systemd unit: {proc.stderr}")

        subprocess.run(
            ["sudo", "systemctl", "daemon-reload"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "systemctl", "enable", "robothor-gateway.service"],
            check=True,
            capture_output=True,
        )
        return unit_path

    def _read_pid(self) -> int | None:
        """Read PID from pidfile."""
        if not self.pidfile.exists():
            return None
        try:
            return int(self.pidfile.read_text().strip())
        except (ValueError, OSError):
            return None
