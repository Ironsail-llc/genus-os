"""exec: printing a secrets file is refused; sourcing it is not.

Measured on this instance over the week to 2026-09-14 (the main agent, exec):

    cat /run/robothor/secrets.env | grep -i "CRM\\|BRIDGE\\|TOKEN" | head -10
    grep -r "PGPASSWORD\\|DB_PASS" <workspace>/.env
    set -a; . /run/robothor/secrets.env; set +a; curl ... https://openrouter...

The first two print credentials into the transcript (the redactor then
cleans up). The third is how an agent runs an authenticated command without
ever seeing the value, and must keep working.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.secret_paths import exec_reads_secret
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import _exec

REFUSED_COMMANDS = [
    'cat /run/robothor/secrets.env | grep -i "CRM\\|BRIDGE\\|TOKEN" | head -10',
    "grep -r 'PGPASSWORD\\|DB_PASS' /home/alice/robothor/.env",
    "head -5 crm/.env",
    "sed -n 1,5p ~/.config/robothor/connectors.env",
    "cat ~/.ssh/id_rsa",
    "cd /home/alice/robothor && cat .env",
    "cat /home/alice/robothor/.env 2>/dev/null | grep -i database",
    "tail -n 20 /etc/robothor/secrets.enc.json",
    "base64 /home/alice/.aws/credentials",
    "less .env.production",
    "awk -F= '{print $2}' secrets.json",
    "printenv",
    "printenv OPENROUTER_API_KEY",
    "env | grep -i token",
    "env",
    "export -p",
]
ALLOWED_COMMANDS = [
    "set -a; . /run/robothor/secrets.env; set +a; curl -s https://api.example.com/v1",
    "[ -f /run/robothor/secrets.env ] && set -a && . /run/robothor/secrets.env && set +a; python x.py",
    "source .env && python scripts/run.py",
    "ls -la /run/robothor",
    "test -f /run/robothor/secrets.env && echo present",
    "wc -l .env",
    "cat README.md",
    "grep -rn TODO robothor/",
    "env FOO=1 python x.py",
    "env -u GH_TOKEN gh pr list",
    "cat infra/robothor.env.example",
    "git status",
    "python -c 'print(1)'",
    "export FOO=1; python x.py",
]


@pytest.mark.parametrize("command", REFUSED_COMMANDS)
def test_commands_that_print_a_secret_are_refused(command: str) -> None:
    assert exec_reads_secret(command), command


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_commands_that_only_use_a_secret_are_allowed(command: str) -> None:
    assert exec_reads_secret(command) is None, command


def test_the_refusal_names_the_file_never_a_value() -> None:
    reason = exec_reads_secret("cat /run/robothor/secrets.env")
    assert reason is not None
    assert "secrets.env" in reason


class TestExecRefuses:
    def test_a_printing_command_never_runs(self, tmp_path) -> None:
        env = tmp_path / ".env"
        env.write_text("ACME_API_KEY=Qx9v2LmT7pRz4Kd1wq\n")
        ctx = ToolContext(agent_id="main", workspace=str(tmp_path))
        out = asyncio.run(_exec({"command": f"cat {env}"}, ctx))
        assert "error" in out and "stdout" not in out
        assert "Qx9v2LmT7pRz4Kd1wq" not in str(out)

    def test_an_ordinary_command_still_runs(self, tmp_path) -> None:
        ctx = ToolContext(agent_id="main", workspace=str(tmp_path))
        out = asyncio.run(_exec({"command": "echo ok"}, ctx))
        assert out["stdout"].strip() == "ok"
