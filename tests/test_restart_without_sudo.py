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

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PATH_UNIT = REPO_ROOT / "infra/systemd/robothor-restart.path"
SVC_UNIT = REPO_ROOT / "infra/systemd/robothor-restart.service"
TELEGRAM_MODULE = REPO_ROOT / "robothor/engine/telegram.py"
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
    # The restart itself moved into the root-owned handler when the broker
    # grew from one hardcoded unit to an allowlist. Follow it there rather
    # than dropping the assertion.
    handler = SVC_UNIT.parent.parent / "bin" / "robothor-restart-handler.sh"
    assert handler.exists(), "the handler the unit executes is missing"
    assert "systemctl restart" in handler.read_text()


def test_restart_service_consumes_the_trigger():
    """A trigger left in place would restart in a loop."""
    handler = (SVC_UNIT.parent.parent / "bin" / "robothor-restart-handler.sh").read_text()
    assert "rm -f" in handler, "the trigger file must be removed, or the path unit re-fires forever"
    # And it must be removed for REFUSED requests too, or an un-allowlisted
    # name loops the path unit just as effectively as an honoured one.
    consume = handler.index("rm -f")
    refuse = handler.index("not in the allowlist")
    assert consume < refuse, "the request must be consumed before it is judged"


def test_restart_service_only_restarts_robothor_units():
    """The agent must not be able to name an arbitrary unit."""
    handler = (SVC_UNIT.parent.parent / "bin" / "robothor-restart-handler.sh").read_text()
    allowed = handler.split("ALLOWED=(")[1].split(")")[0]
    names = [n.strip() for n in allowed.split() if n.strip()]
    assert names, "the allowlist is empty"
    assert all(n.startswith("robothor-") for n in names), (
        f"a non-robothor unit is agent-restartable: {names}"
    )
    # The unit name must come from the FILENAME, never the file's contents.
    assert "$(cat" not in handler and "$(<" not in handler, (
        "the unit to restart must NOT be read from the agent-writable trigger "
        "file — that would let an agent restart (or stop) any unit on the box"
    )


def test_handler_writes_the_trigger_not_sudo():
    """The Telegram /restart handler must use the trigger-file mechanism
    above, not shell out to `sudo systemd-run`.

    Regression guard for the silent no-op: under the live NoNewPrivileges
    sandbox, `sudo -n systemd-run ...` launched via Popen with all fds
    DEVNULL and never awaited dies unseen in the child — the handler replied
    "Restarting..." and then did nothing. This asserts the handler region
    contains no "sudo" at all, so that failure mode cannot come back.
    """
    src = TELEGRAM_MODULE.read_text()
    match = re.search(
        r"async def _handle_restart_command\(.*?\n(?=    async def |\Z)",
        src,
        re.DOTALL,
    )
    assert match, "_handle_restart_command not found in robothor/engine/telegram.py"
    handler_src = match.group(0)
    assert "sudo" not in handler_src.lower(), (
        "the restart handler must not shell out to sudo — it must write the "
        "trigger file that robothor-restart.path watches"
    )
    assert "systemd-run" not in handler_src, (
        "the restart handler must not invoke systemd-run directly — that path "
        "is owned by robothor-restart.service, triggered via the trigger file"
    )
    assert "_RESTART_TRIGGERS" in handler_src, (
        "the restart handler must consult the injectable _RESTART_TRIGGERS map"
    )
