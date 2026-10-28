"""``read_file`` on a plain dotenv file returns it with the secrets masked (GEN-42).

The operator's assistant could not read its own settings: every ``*.env`` was
refused outright, so "what is X set to on this box?" had no answer short of
``exec``. A plain ``KEY=VALUE`` file is now returned with every key visible,
non-secret values visible, and secret values reduced to ``****`` plus the last
four characters (short values fully masked). Encrypted files, key files and
everything else ``secret_paths`` refuses stay refused.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS

_read_file = HANDLERS["read_file"]

# Generic, fake, credential-SHAPED values. None of them is a real credential.
FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_PW = "Zr8qLm2Vx7Tn4Kp9"
FAKE_DSN_PW = "s3cr3tPassw0rdXyz"


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(agent_id="main", workspace=str(workspace))


def _read(path: Path, workspace: Path) -> dict:
    return asyncio.run(_read_file({"path": str(path)}, _ctx(workspace)))


ENV_BODY = (
    "# instance settings\n"
    "ROBOTHOR_DEFAULT_TENANT=default\n"
    "ROBOTHOR_LOG_LEVEL=INFO\n"
    f"GITHUB_TOKEN={FAKE_GH}\n"
    f'DB_PASSWORD="{FAKE_PW}"\n'
    f"DATABASE_URL=postgresql://genus:{FAKE_DSN_PW}@db.example.com:5432/genus\n"
    "SHORT_SECRET=abc\n"
    "EMPTY_API_KEY=\n"
    "export ACME_MODE=fast\n"
)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    f = tmp_path / "robothor.env"
    f.write_text(ENV_BODY)
    return f


class TestMaskedDotenvRead:
    def test_keys_and_non_secret_values_are_visible(self, env_file: Path, tmp_path: Path) -> None:
        out = _read(env_file, tmp_path)
        assert "error" not in out, out
        content = out["content"]
        assert "ROBOTHOR_DEFAULT_TENANT=default" in content
        assert "ROBOTHOR_LOG_LEVEL=INFO" in content
        assert "ACME_MODE=fast" in content
        for key in ("GITHUB_TOKEN", "DB_PASSWORD", "DATABASE_URL", "SHORT_SECRET", "EMPTY_API_KEY"):
            assert key in content
        assert out.get("masked") is True

    def test_secret_values_are_masked_to_the_last_four(
        self, env_file: Path, tmp_path: Path
    ) -> None:
        content = _read(env_file, tmp_path)["content"]
        assert FAKE_GH not in content
        assert f"GITHUB_TOKEN=****{FAKE_GH[-4:]}" in content
        assert FAKE_PW not in content
        assert f'DB_PASSWORD="****{FAKE_PW[-4:]}"' in content

    def test_a_credential_in_a_url_is_masked_by_value(self, env_file: Path, tmp_path: Path) -> None:
        content = _read(env_file, tmp_path)["content"]
        assert FAKE_DSN_PW not in content
        assert "DATABASE_URL=****" in content

    def test_short_secret_values_are_fully_masked(self, env_file: Path, tmp_path: Path) -> None:
        content = _read(env_file, tmp_path)["content"]
        assert "SHORT_SECRET=****\n" in content
        assert "abc" not in content.replace("ACME", "")

    def test_an_empty_secret_shows_as_empty(self, env_file: Path, tmp_path: Path) -> None:
        assert "EMPTY_API_KEY=\n" in _read(env_file, tmp_path)["content"]

    def test_a_credential_shaped_value_under_an_innocent_name_is_masked(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "connectors.env"
        f.write_text(f"UPSTREAM_THING={FAKE_GH}\n")
        content = _read(f, tmp_path)["content"]
        assert FAKE_GH not in content
        assert "UPSTREAM_THING=****" in content

    def test_a_declared_secret_setting_is_masked_whatever_its_name(self, tmp_path: Path) -> None:
        from robothor.settings.registry import field_index

        secret_names = sorted({r["env"] for r in field_index().values() if r["secret"]})
        assert secret_names, "the settings registry declares no secret at all?"
        name = secret_names[0]
        f = tmp_path / "robothor.env"
        f.write_text(f"{name}=plainlookingvalue123\n")
        content = _read(f, tmp_path)["content"]
        assert "plainlookingvalue123" not in content
        assert f"{name}=****e123" in content

    def test_a_credential_in_a_comment_is_not_shown(self, tmp_path: Path) -> None:
        f = tmp_path / ".env"
        f.write_text(f"# old: GITHUB_TOKEN={FAKE_GH}\nX=1\n")
        content = _read(f, tmp_path)["content"]
        assert FAKE_GH not in content
        assert "X=1" in content


class TestStillRefused:
    def test_a_sops_dotenv_is_refused(self, tmp_path: Path) -> None:
        f = tmp_path / "robothor.env"
        f.write_text(
            "GITHUB_TOKEN=ENC[AES256_GCM,data:abcd,iv:efgh,tag:ijkl,type:str]\nsops_version=3.8.1\n"
        )
        out = _read(f, tmp_path)
        assert "content" not in out
        assert "secrets file" in out["error"]

    def test_a_sops_json_file_is_refused(self, tmp_path: Path) -> None:
        f = tmp_path / "secrets.enc.json"
        f.write_text('{"a": "ENC[...]"}')
        out = _read(f, tmp_path)
        assert "content" not in out and "secrets file" in out["error"]

    def test_a_secrets_named_dotenv_is_refused(self, tmp_path: Path) -> None:
        f = tmp_path / "secrets.env"
        f.write_text("X=1\n")
        assert "content" not in _read(f, tmp_path)

    @pytest.mark.parametrize("name", ["server.pem", "id_rsa", "tls.key", "credentials.json"])
    def test_key_files_are_refused(self, tmp_path: Path, name: str) -> None:
        f = tmp_path / name
        f.write_text("X=1\n")
        out = _read(f, tmp_path)
        assert "content" not in out and "secrets file" in out["error"]

    def test_a_dotenv_inside_a_secret_directory_is_refused(self, tmp_path: Path) -> None:
        d = tmp_path / ".aws"
        d.mkdir()
        f = d / "prod.env"
        f.write_text("X=1\n")
        assert "content" not in _read(f, tmp_path)

    def test_a_file_that_is_not_plain_dotenv_is_refused(self, tmp_path: Path) -> None:
        """A PEM pasted into an .env (or any non KEY=VALUE body) is not maskable."""
        f = tmp_path / ".env"
        f.write_text("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n")
        out = _read(f, tmp_path)
        assert "content" not in out
        assert "MIIEvQ" not in str(out)

    def test_exec_side_checks_are_unchanged(self) -> None:
        from robothor.engine.secret_paths import exec_reads_secret

        assert exec_reads_secret("cat /etc/robothor/robothor.env")


class TestPathPredicate:
    @pytest.mark.parametrize(
        "path",
        [".env", "crm/.env", ".env.local", "robothor.env", "/etc/robothor/robothor.env"],
    )
    def test_plain_dotenv_names_are_maskable(self, path: str) -> None:
        from robothor.engine.secret_paths import is_maskable_dotenv_path

        assert is_maskable_dotenv_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "secrets.env",
            "secrets.enc.yaml",
            "x.enc.json",
            "id_rsa",
            "server.pem",
            ".ssh/config.env",
            ".robothor/secrets/a.env",
            ".netrc",
            "notes.md",
        ],
    )
    def test_everything_else_is_not(self, path: str) -> None:
        from robothor.engine.secret_paths import is_maskable_dotenv_path

        assert not is_maskable_dotenv_path(path)
