"""The engine must not be able to become root.

The engine runs as systemd User=philip. `philip` is in the sudo group WITH
PASSWORDLESS sudo (`sudo -n true` succeeds). Six agents hold `exec` with no
allowlist at all — main, conversation-inbox, crm-hygiene, vision-monitor,
auto-researcher, email-analyst — so a prompt-injected agent runs
`sudo <anything>` and owns the box: SSH keys, gog OAuth tokens, secrets, the
lot.

`NoNewPrivileges=yes` closes that in one line: no setuid binary (sudo included)
can raise privileges for the service or any of its children, whatever sudoers
says. It is the single highest-leverage control available on this box, and it
is independent of the container work.

These tests pin the unit's hardening so it cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UNITS = [
    "infra/systemd/robothor-engine.service",
    "infra/systemd/robothor-bridge.service",
]

# The directives that actually contain a compromised agent.
REQUIRED = [
    "NoNewPrivileges=yes",  # blocks sudo/setuid escalation — the whole point
    "PrivateTmp=yes",
    "RestrictSUIDSGID=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectControlGroups=yes",
]


def _unit(name: str) -> str:
    path = REPO_ROOT / name
    return path.read_text() if path.exists() else ""


def test_engine_blocks_privilege_escalation():
    src = _unit("infra/systemd/robothor-engine.service")
    assert src, "engine unit missing"
    assert "NoNewPrivileges=yes" in src, (
        "the engine can escalate to root: it runs as a user with passwordless "
        "sudo, and six agents hold unrestricted `exec`. NoNewPrivileges=yes "
        "blocks every setuid path (sudo included) for the service and its "
        "children."
    )


def test_engine_carries_the_full_hardening_set():
    src = _unit("infra/systemd/robothor-engine.service")
    missing = [d for d in REQUIRED if d not in src]
    assert not missing, f"engine unit is missing hardening directives: {missing}"


def test_engine_confines_the_filesystem():
    src = _unit("infra/systemd/robothor-engine.service")
    assert "ProtectSystem=" in src, "engine can write anywhere on the filesystem"
    assert "ProtectHome=" in src, (
        "engine has full write access to $HOME — SSH keys, gog OAuth tokens, secrets"
    )
    # confinement is useless without the workspace explicitly re-opened
    assert "ReadWritePaths=" in src, (
        "ProtectSystem/ProtectHome without ReadWritePaths will break the engine — "
        "the workspace must be explicitly writable"
    )


def test_every_long_running_unit_is_hardened():
    unhardened = [u for u in UNITS if _unit(u) and "NoNewPrivileges=yes" not in _unit(u)]
    assert not unhardened, f"these units can still escalate: {unhardened}"
