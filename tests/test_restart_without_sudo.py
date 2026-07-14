"""Agents must be able to restart services without sudo.

NoNewPrivileges blocks every setuid path, sudo included — which is the point,
because the engine user has passwordless sudo and six agents hold unrestricted
`exec`. But it also blocks the one legitimate use we found in 30 days of
history: an agent running `sudo systemctl restart robothor-engine` to heal the
fleet.

The operator's standing rule is that Robothor does things itself. So the
capability is preserved without the escalation path: the agent writes a trigger
file, and a systemd .path unit runs the restart as root. systemd holds the
privilege; the agent never gains it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PATH_UNIT = REPO_ROOT / "infra/systemd/robothor-restart.path"
SVC_UNIT = REPO_ROOT / "infra/systemd/robothor-restart.service"
TRIGGER = "/run/robothor/restart-request"


def test_path_unit_watches_the_trigger_file():
    assert PATH_UNIT.exists(), "robothor-restart.path missing"
    src = PATH_UNIT.read_text()
    assert TRIGGER in src, f"path unit must watch {TRIGGER}"
    assert "PathExists=" in src or "PathModified=" in src


def test_restart_service_runs_as_root_and_is_oneshot():
    assert SVC_UNIT.exists(), "robothor-restart.service missing"
    src = SVC_UNIT.read_text()
    assert "Type=oneshot" in src
    # No User= line means root — systemd holds the privilege, not the agent.
    assert "\nUser=" not in src, (
        "the restart unit must run as root; that is the whole point — the agent "
        "never gains privilege, systemd exercises it"
    )
    assert "systemctl restart" in src


def test_restart_service_consumes_the_trigger():
    """A trigger left in place would restart in a loop."""
    src = SVC_UNIT.read_text()
    assert "rm -f" in src or "ConditionPathExists" in src, (
        "the trigger file must be removed, or the path unit re-fires forever"
    )


def test_restart_service_only_restarts_robothor_units():
    """The agent must not be able to name an arbitrary unit."""
    src = SVC_UNIT.read_text()
    assert "robothor-" in src, "restart target must be pinned to robothor units"
    assert "$(cat" not in src and "${" not in src.split("ExecStart")[-1].split("\n")[0], (
        "the unit to restart must NOT be read from the agent-writable trigger "
        "file — that would let an agent restart (or stop) any unit on the box"
    )
