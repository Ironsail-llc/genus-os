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


# --- engine service.d drop-in mirrors ---------------------------------------
#
# infra/systemd/robothor-engine.service.d/ carries the engine drop-ins as
# TEMPLATES (canonical placeholder spellings per infra/systemd/README.md,
# rendered by scripts/render-unit.sh; check_dropin_drift.sh renders before
# diffing against live). The live hardening.conf grants
# ReadWritePaths=/mnt/robothor-backup: undocumented, and no code under
# robothor/ touches that mount (the backup units run as their own systemd
# services) — so write access there is pure prompt-injected-agent blast
# radius over 238GB of backups. The mirror deliberately does NOT carry that
# line forward; the live-side removal follows in a controlled window.
DROPIN_DIR = REPO_ROOT / "infra" / "systemd" / "robothor-engine.service.d"


def _dropin_mirrors() -> dict[str, str]:
    if not DROPIN_DIR.exists():
        return {}
    return {p.name: p.read_text() for p in DROPIN_DIR.glob("*.conf")}


def test_dropin_mirrors_grant_no_backup_mount_access():
    grants = []
    for name, text in _dropin_mirrors().items():
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ReadWritePaths=") and "robothor-backup" in stripped:
                grants.append(f"{name}: {stripped}")
    assert not grants, (
        f"engine drop-in mirror(s) still grant write access to the backup mount: {grants} "
        "— this is the exact security invariant this guard exists to enforce"
    )


def test_dropin_hardening_mirror_keeps_the_core_posture():
    text = _dropin_mirrors().get("hardening.conf", "")
    assert text, "infra/systemd/robothor-engine.service.d/hardening.conf mirror missing"
    for directive in ("NoNewPrivileges=yes", "ProtectSystem=strict"):
        assert directive in text, f"hardening.conf mirror regressed: missing {directive!r}"


def test_dropin_sandbox_mirror_exists_and_documents_why_paths_are_absolute():
    text = _dropin_mirrors().get("zz-sandbox.conf", "")
    assert text, "infra/systemd/robothor-engine.service.d/zz-sandbox.conf mirror missing"
    assert "%h" in text, (
        "zz-sandbox.conf mirror should keep the live file's own explanation of "
        "why it uses absolute paths instead of %h"
    )


# --- owner host execution -----------------------------------------------------
#
# robothor-host-exec.service is the verified-owner path: the engine hands a
# command to it only after the owner check passes, and it is how the main agent
# operates its own computer. ProtectHome=read-only there made all of /home
# read-only for every owner command -- the repo, ~/.config, CLI token caches --
# so a calendar CLI that had already written the event reported "Read-only
# file system" and the agent diagnosed a failing disk. Confinement belongs on
# the engine (non-owner, cron and sub-agent runs), not on the owner's shell.


def _service_directives(src: str) -> list[str]:
    return [
        line.strip()
        for line in src.splitlines()
        if line.strip() and not line.strip().startswith(("#", ";"))
    ]


def test_host_exec_can_write_the_home_directory():
    src = _unit("infra/systemd/robothor-host-exec.service")
    assert src, "host-exec unit missing"
    directives = _service_directives(src)
    protect = [d for d in directives if d.startswith("ProtectHome=")]
    assert protect == ["ProtectHome=no"], (
        f"host-exec must leave /home writable for the verified owner, got {protect}"
    )


def test_host_exec_dropins_do_not_reimpose_protect_home():
    dropins = REPO_ROOT / "infra" / "systemd" / "robothor-host-exec.service.d"
    offenders = [
        f"{p.name}: {d}"
        for p in sorted(dropins.glob("*.conf"))
        for d in _service_directives(p.read_text())
        if d.startswith("ProtectHome=") and d != "ProtectHome=no"
    ]
    assert not offenders, f"a host-exec drop-in makes /home read-only again: {offenders}"


def test_engine_stays_confined_while_host_exec_is_open():
    engine = _unit("infra/systemd/robothor-engine.service")
    assert "ProtectHome=read-only" in _service_directives(engine), (
        "only the owner path opens /home; the engine keeps ProtectHome=read-only"
    )
