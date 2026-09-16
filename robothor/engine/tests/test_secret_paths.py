"""Agents never read credential files.

Measured on this instance over the 48 hours to 2026-09-14: sub-agents of
agent-architect, auto-researcher, auto-agent, devops-analyst and
email-responder called ``read_file`` on ``~/.config/robothor/connectors.env``,
the workspace ``.env``, ``crm/.env``, a config backup's ``robothor.env`` and
``/etc/robothor/secrets.enc.json``. The output redactor caught the values
(those are the "credential warnings" the operator saw), but the read itself
should never happen: credentials reach tools through the platform, and a
model has no use for the file that holds them. ``read_file`` refuses these
paths before opening them.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from robothor.engine.secret_paths import is_secret_path
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import _read_file

SECRET = [
    "/srv/app/.config/robothor/connectors.env",
    "/srv/app/robothor/.env",
    "crm/.env",
    ".env.local",
    ".env.production",
    ".config-backup-20260714/robothor.env",
    "/etc/robothor/secrets.enc.json",
    "/run/robothor/secrets.env",
    "/srv/app/robothor/.robothor/secrets.yaml",
    "secrets.json",
    "infra/secrets-prod.yaml",
    "/srv/app/.ssh/id_ed25519",
    "/srv/app/.ssh/id_rsa.pub",
    "/srv/app/.aws/credentials",
    "/srv/app/.kube/config",
    "/srv/app/.netrc",
    "/srv/app/.pgpass",
    # git's plaintext credential store, MySQL/boto/s3cmd client files, htpasswd,
    # the Vault CLI token: every one is a file whose only content is a secret.
    "~/.git-credentials",
    "/srv/app/.git-credentials",
    "/srv/app/.my.cnf",
    "/srv/app/.boto",
    "/srv/app/.s3cfg",
    "/srv/app/site/.htpasswd",
    "/srv/app/.vault-token",
    # git's plaintext credential store, MySQL/boto/s3cmd client files, htpasswd,
    # the Vault CLI token: every one is a file whose only content is a secret.
    "~/.git-credentials",
    "/srv/app/.git-credentials",
    "/srv/app/.my.cnf",
    "/srv/app/.boto",
    "/srv/app/.s3cfg",
    "/srv/app/site/.htpasswd",
    "/srv/app/.vault-token",
    "certs/server.pem",
    "certs/server.key",
    "gcp/service-account.json",
    "credentials.json",
    "token.json",
    "vault.kdbx",
    # The channel inbox's quarantine directory. A `.env` the operator sent over
    # Telegram is stored as `<file_unique_id>-env`, which no name rule can
    # recognise, so the verdict is taken at save time and recorded as the
    # DIRECTORY the file is kept in.
    "/srv/app/inbox/telegram/100200300/2026-09-16/secret/AgAC-xQ-env",
    "inbox/telegram/100200300/2026-09-16/secret/BQAD-77-credentials.json",
    # Including when the sanitised name ends in a documentation suffix: the
    # operator sent their credentials as `notes.md` and the quarantine outranks
    # the "examples stay readable" rule.
    "inbox/telegram/100200300/2026-09-16/secret/CQAE-11-notes.md",
]
NOT_SECRET = [
    "/srv/app/robothor/README.md",
    "docs/agents/main.yaml",
    "infra/robothor.env.example",
    "infra/systemd/robothor.env.example",
    "templates/owner.yaml.example",
    "robothor/engine/runner.py",
    "brain/agents/main.md",
    "environment.md",
    "notes/keynote.md",
    "src/tokenizer.py",
    "data/environment_2026.csv",
    "docs/secrets.md",
    # Operational state in the platform's runtime directories is not a secret;
    # a week's replay of real commands showed these as the only false refusals.
    "/run/robothor/model-breaker-alerts.json",
    "/run/robothor/slo-state.json",
    "/run/robothor/alerts.log",
    "/etc/robothor/robothor.conf",
    # The inbox rule is a SHAPE, and these are the near misses. Re-review R3
    # found the loose version refusing the first of them.
    "projects/inbox/secret/design.md",
    "inbox/secret/design.md",
    "inbox/telegram/100200300/2026-09-16/AgAC-xQ-photo.jpg",  # not quarantined
    "inbox/telegram/100200300/latest/secret/AgAC-xQ-env",  # no date, not ours
    "secret/notes.md",
]


@pytest.mark.parametrize("path", SECRET)
def test_credential_files_are_secret_paths(path: str) -> None:
    assert is_secret_path(path), path


@pytest.mark.parametrize("path", NOT_SECRET)
def test_ordinary_files_are_not(path: str) -> None:
    assert not is_secret_path(path), path


def test_examples_and_docs_stay_readable_even_when_named_like_secrets() -> None:
    assert not is_secret_path("infra/robothor.env.example")
    assert not is_secret_path("docs/secrets.md")


class TestReadFileRefuses:
    def _ctx(self, workspace: Path) -> ToolContext:
        return ToolContext(agent_id="agent-architect", workspace=str(workspace))

    def test_a_secrets_file_is_refused_before_it_is_opened(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("ACME_API_KEY=Qx9v2LmT7pRz4Kd1wq\n")
        out = asyncio.run(_read_file({"path": str(env)}, self._ctx(tmp_path)))
        assert "error" in out
        assert "content" not in out
        assert "Qx9v2LmT7pRz4Kd1wq" not in str(out)
        assert "secrets file" in out["error"]

    def test_a_relative_secrets_path_under_the_workspace_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "crm").mkdir()
        (tmp_path / "crm" / ".env").write_text("X=1\n")
        out = asyncio.run(_read_file({"path": "crm/.env"}, self._ctx(tmp_path)))
        assert "error" in out and "content" not in out

    def test_a_missing_secrets_file_is_still_refused_not_reported_missing(
        self, tmp_path: Path
    ) -> None:
        """The refusal must not leak whether the file exists."""
        out = asyncio.run(_read_file({"path": str(tmp_path / "nope.env")}, self._ctx(tmp_path)))
        assert "secrets file" in out["error"]

    def test_an_ordinary_file_is_still_read(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.md"
        f.write_text("hello\n")
        out = asyncio.run(_read_file({"path": str(f)}, self._ctx(tmp_path)))
        assert out["content"] == "hello\n"

    def test_the_example_env_is_still_read(self, tmp_path: Path) -> None:
        f = tmp_path / "robothor.env.example"
        f.write_text("ACME_API_KEY=\n")
        out = asyncio.run(_read_file({"path": str(f)}, self._ctx(tmp_path)))
        assert out["content"] == "ACME_API_KEY=\n"
