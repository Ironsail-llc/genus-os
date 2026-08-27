"""Say so when you are operating on someone else's instance.

`robothor.cli.main()` calls `load_instance_env()`, which reads
/etc/robothor/robothor.env so a CLI run inherits the daemon's guardrail flags
rather than silently reading them as off. That is correct and load-bearing.

But it also adopts ROBOTHOR_WORKSPACE. Verified on this box: a fresh clone of
the public repo, installed into a temp venv, run with `env -i` and a different
HOME, reported

    PostgreSQL:  Connected — 118 tables
    Vault:       15 secret(s) stored
    Workspace:   /home/<operator>/robothor

— the operator's instance, not the caller's. Postgres peer auth over the Unix
socket supplies the database; the system env file supplies the workspace.

On a single-operator box that is intended. For a platform other people install,
a second user on a shared machine gets the first user's workspace, database and
vault with nothing saying so. The fix is not to stop adopting the env — that
would reintroduce the guardrail-bypass this was added to prevent — but to say
plainly which instance you are attached to and whose it is.
"""

from __future__ import annotations

from pathlib import Path

from robothor.cli.admin import describe_instance_ownership


def test_silent_when_the_workspace_is_your_own(tmp_path):
    home = tmp_path / "me"
    ws = home / "robothor"
    ws.mkdir(parents=True)
    assert describe_instance_ownership(ws, home=home) is None


def test_warns_when_the_workspace_is_outside_your_home(tmp_path):
    home = tmp_path / "me"
    home.mkdir()
    foreign = tmp_path / "someone-else" / "robothor"
    foreign.mkdir(parents=True)

    msg = describe_instance_ownership(foreign, home=home)

    assert msg is not None, "attached to a workspace outside HOME with no warning"
    assert str(foreign) in msg
    assert "ROBOTHOR_WORKSPACE" in msg, "the message must say how to change it"


def test_the_message_names_the_source_of_the_override(tmp_path):
    """An operator has to know WHERE the value came from to change it."""
    home = tmp_path / "me"
    home.mkdir()
    foreign = tmp_path / "other" / "robothor"
    foreign.mkdir(parents=True)

    msg = describe_instance_ownership(
        foreign, home=home, env_file=Path("/etc/robothor/robothor.env")
    )

    assert "/etc/robothor/robothor.env" in msg


def test_a_missing_home_does_not_crash_status(tmp_path):
    """Path.home() raises with no HOME and no passwd entry — a real k8s case.
    status must still print, not traceback."""
    foreign = tmp_path / "other" / "robothor"
    foreign.mkdir(parents=True)
    assert describe_instance_ownership(foreign, home=None) is None
