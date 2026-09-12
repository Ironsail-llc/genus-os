"""Tests for robothor.setup — init wizard."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from robothor.config import DatabaseConfig, OllamaConfig, RedisConfig
from robothor.setup import (
    REQUIRED_MODELS,
    check_prerequisites,
    create_workspace,
    generate_docker_compose,
    pull_ollama_models,
    run_init,
    run_migration,
    write_env_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestCheckPrerequisites:
    def test_always_finds_python(self):
        results = check_prerequisites()
        python = next(r for r in results if r["name"] == "Python")
        assert python["found"] is True
        assert python["required"] is True

    def test_missing_psql(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: None if name == "psql" else "/usr/bin/" + name
        )
        results = check_prerequisites()
        psql = next(r for r in results if "PostgreSQL" in r["name"])
        assert psql["found"] is False
        assert psql["required"] is False
        assert psql["hint"]  # has install hint

    def test_missing_redis_cli(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: None if name == "redis-cli" else "/usr/bin/" + name
        )
        results = check_prerequisites()
        redis = next(r for r in results if "Redis" in r["name"])
        assert redis["found"] is False

    def test_docker_required_when_flag_set(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        results = check_prerequisites(docker_required=True)
        docker = next(r for r in results if r["name"] == "Docker")
        assert docker["required"] is True
        assert docker["found"] is False

    def test_docker_not_required_by_default(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        results = check_prerequisites(docker_required=False)
        docker = next(r for r in results if r["name"] == "Docker")
        assert docker["required"] is False

    def test_ollama_unreachable(self, monkeypatch):
        """When Ollama is down, should report not reachable."""
        import httpx

        import robothor.setup as setup_mod

        monkeypatch.setattr(
            setup_mod.httpx,
            "get",
            MagicMock(side_effect=httpx.ConnectError("refused")),
        )
        results = check_prerequisites()
        ollama = next(r for r in results if r["name"] == "Ollama")
        assert ollama["found"] is False
        assert "not reachable" in ollama["detail"]

    def test_returns_five_entries(self):
        results = check_prerequisites()
        assert len(results) == 5
        names = {r["name"] for r in results}
        assert "Python" in names
        assert "Docker" in names
        assert "Ollama" in names


class TestPromptDbConfig:
    def test_yes_mode_uses_env_defaults(self, monkeypatch):
        """With --yes, db config should come from env without prompting."""
        monkeypatch.setenv("ROBOTHOR_DB_HOST", "dbhost.example.com")
        monkeypatch.setenv("ROBOTHOR_DB_PORT", "5433")
        monkeypatch.setenv("ROBOTHOR_DB_NAME", "testdb")
        monkeypatch.setenv("ROBOTHOR_DB_USER", "testuser")
        monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "secret")

        from robothor.setup import _db_config_from_env

        cfg = _db_config_from_env()
        assert cfg.host == "dbhost.example.com"
        assert cfg.port == 5433
        assert cfg.name == "testdb"
        assert cfg.user == "testuser"
        assert cfg.password == "secret"


class TestCreateWorkspace:
    def test_creates_directories(self, tmp_path):
        workspace = tmp_path / "robothor"
        create_workspace(workspace)
        assert workspace.is_dir()
        assert (workspace / "memory").is_dir()
        assert (workspace / "faces").is_dir()

    def test_idempotent(self, tmp_path):
        workspace = tmp_path / "robothor"
        create_workspace(workspace)
        create_workspace(workspace)  # should not raise
        assert workspace.is_dir()


class TestWriteEnvFile:
    def test_writes_all_vars(self, tmp_path):
        env_path = tmp_path / ".env"
        db = DatabaseConfig(host="myhost", port=5433, name="mydb", user="me", password="pw")
        redis = RedisConfig(host="redis-host", port=6380)
        ollama = OllamaConfig(host="ollama-host", port=11435)

        wrote = write_env_file(
            env_path,
            db,
            redis,
            ollama,
            owner_name="Alice",
            ai_name="Jarvis",
            yes=True,
        )
        assert wrote is True

        content = env_path.read_text()
        # Operator identity lives in ~/.robothor/owner.yaml, never in .env:
        # two files claiming the same identity is how an instance ends up
        # answering to one name and filing CRM rows under another.
        assert "ROBOTHOR_OWNER_NAME" not in content
        assert "ROBOTHOR_OWNER_EMAIL" not in content
        assert "ROBOTHOR_AI_NAME=Jarvis" in content
        assert "ROBOTHOR_DB_HOST=myhost" in content
        assert "ROBOTHOR_DB_PORT=5433" in content
        assert "ROBOTHOR_DB_NAME=mydb" in content
        assert "ROBOTHOR_DB_USER=me" in content
        assert "ROBOTHOR_DB_PASSWORD=" in content  # password never written to .env
        assert "ROBOTHOR_REDIS_HOST=redis-host" in content
        assert "ROBOTHOR_REDIS_PORT=6380" in content
        assert "ROBOTHOR_OLLAMA_HOST=ollama-host" in content
        assert "ROBOTHOR_OLLAMA_PORT=11435" in content
        assert f"ROBOTHOR_WORKSPACE={tmp_path}" in content

    def test_preserves_existing_when_declined(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=value\n")

        monkeypatch.setattr("builtins.input", lambda prompt: "n")

        db = DatabaseConfig()
        redis = RedisConfig()
        ollama = OllamaConfig()

        wrote = write_env_file(env_path, db, redis, ollama, yes=False)
        assert wrote is False
        assert env_path.read_text() == "EXISTING=value\n"

    def test_overwrites_when_confirmed(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=value\n")

        monkeypatch.setattr("builtins.input", lambda prompt: "y")

        db = DatabaseConfig()
        redis = RedisConfig()
        ollama = OllamaConfig()

        wrote = write_env_file(env_path, db, redis, ollama, yes=False)
        assert wrote is True
        assert "ROBOTHOR_DB_HOST" in env_path.read_text()


class TestGenerateDockerCompose:
    def test_generates_valid_compose(self, tmp_path):
        compose_path = generate_docker_compose(tmp_path, "mypassword")
        assert compose_path == tmp_path / "docker-compose.yml"
        assert compose_path.exists()

        content = compose_path.read_text()
        assert "postgres" in content
        assert "redis" in content
        assert "ollama" in content
        assert "mypassword" in content
        assert "pgvector/pgvector:pg16" in content


class TestRunMigration:
    def test_mocked_migration(self):
        """Migration should execute SQL and return table count."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (17,)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.apply", return_value=["001_init"]) as mock_apply,
        ):
            db = DatabaseConfig(host="localhost", password="test")
            count = run_migration(db)

        assert count == 17
        mock_apply.assert_called_once_with(connection=mock_conn)
        assert mock_cur.execute.call_count == 1  # table count query

    def test_connection_failure_returns_negative(self):
        """If DB is unreachable, should return -1."""
        with patch("psycopg2.connect", side_effect=Exception("Connection refused")):
            db = DatabaseConfig(host="nonexistent")
            count = run_migration(db)
        assert count == -1

    def test_migration_runner_failure_returns_negative(self):
        """If the canonical runner fails, setup should return -1."""
        mock_conn = MagicMock()
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.apply", side_effect=RuntimeError("drift")),
        ):
            db = DatabaseConfig()
            count = run_migration(db)
        assert count == -1
        mock_conn.close.assert_called_once()

    def test_migration_safety_finding_is_surfaced_not_flattened(self, capsys):
        """A safety refusal must not read as "check your connection settings".

        The caller prints host/port/user advice for a -1, which sends the
        operator hunting a network problem when the migrator actually refused
        on purpose and named the remedy.
        """
        from robothor.db.migrate import BASELINE_UNADOPTED_MESSAGE, MigrationHistoryError

        mock_conn = MagicMock()
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch(
                "robothor.db.migrate.apply",
                side_effect=MigrationHistoryError(BASELINE_UNADOPTED_MESSAGE),
            ),
        ):
            count = run_migration(DatabaseConfig())

        from robothor.setup import MIGRATION_SAFETY_FAILURE

        assert count == MIGRATION_SAFETY_FAILURE
        assert count < 0  # still a failure to the caller's `>= 0` check
        out = capsys.readouterr().out
        assert "ledger empty but schema present" in out
        assert "--adopt-baseline" in out

    def test_a_plain_failure_is_still_the_generic_negative(self):
        """Only a safety refusal gets the distinct code; everything else is -1."""
        mock_conn = MagicMock()
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.apply", side_effect=RuntimeError("boom")),
        ):
            assert run_migration(DatabaseConfig()) == -1

    def test_run_init_adds_no_connection_advice_to_a_safety_refusal(self, tmp_path, capsys):
        """The distinct return code has to actually change what the wizard prints.

        Otherwise the operator reads "check connection settings / Host / Database
        / User" under a message that already told them to run --adopt-through,
        and goes looking for a network fault that does not exist.
        """
        from robothor.setup import MIGRATION_SAFETY_FAILURE

        args = SimpleNamespace(
            yes=True,
            docker=False,
            skip_models=True,
            skip_db=False,
            workspace=str(tmp_path / "workspace"),
        )
        with (
            patch("robothor.setup.check_prerequisites", return_value=[]),
            patch(
                "robothor.setup.run_migration", return_value=MIGRATION_SAFETY_FAILURE
            ) as mock_run,
        ):
            rc = run_init(args)

        assert rc == 1
        mock_run.assert_called_once()
        out = capsys.readouterr().out
        assert "Check connection settings" not in out
        assert "Host:" not in out
        assert "refused" in out


class TestPullModels:
    def test_connection_error_handled(self, capsys, monkeypatch):
        """Should handle Ollama being unreachable gracefully."""
        import httpx

        import robothor.setup as setup_mod

        monkeypatch.setattr(
            setup_mod.httpx,
            "stream",
            MagicMock(side_effect=httpx.ConnectError("refused")),
        )
        pull_ollama_models("http://localhost:11434", ["test-model:latest"])

        out = capsys.readouterr().out
        assert "Pulling test-model:latest" in out
        assert "failed" in out

    def test_required_models_list(self):
        """REQUIRED_MODELS should have the two RAG models."""
        assert "qwen3-embedding:0.6b" in REQUIRED_MODELS
        assert "Qwen3-Reranker-0.6B:F16" in REQUIRED_MODELS


class TestRunInit:
    def test_yes_skip_all(self, tmp_path, capsys, monkeypatch):
        """--yes --skip-models --skip-db should create workspace + env only."""
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True,
            docker=False,
            skip_models=True,
            skip_db=True,
            workspace=str(workspace),
        )

        # Prevent Ollama probe from hitting network
        import robothor.setup as setup_mod

        monkeypatch.setattr(
            setup_mod.httpx,
            "get",
            MagicMock(side_effect=Exception("no network")),
        )

        rc = run_init(args)
        assert rc == 0
        assert workspace.is_dir()
        assert (workspace / ".env").exists()
        assert (workspace / "memory").is_dir()
        assert (workspace / "faces").is_dir()

        out = capsys.readouterr().out
        assert "Genus OS initialized" in out

    def test_missing_required_prereq_exits(self, capsys, monkeypatch):
        """If a required prerequisite is missing, should exit with code 1."""
        # Make check_prerequisites return a missing required item
        fake_prereqs = [
            {"name": "Python", "found": True, "detail": "3.12", "required": True, "hint": ""},
            {
                "name": "Docker",
                "found": False,
                "detail": "not found",
                "required": True,
                "hint": "install docker",
            },
        ]
        monkeypatch.setattr("robothor.setup.check_prerequisites", lambda **kw: fake_prereqs)

        args = SimpleNamespace(
            yes=True, docker=True, skip_models=True, skip_db=True, workspace="/tmp/test"
        )
        rc = run_init(args)
        assert rc == 1
        assert "Cannot continue" in capsys.readouterr().out


class TestIdentityEnvVars:
    def test_yes_mode_uses_identity_env_vars(self, tmp_path, capsys, monkeypatch):
        """With --yes, identity should come from env vars."""
        workspace = tmp_path / "robothor"
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "Alice")
        monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "alice@example.com")
        monkeypatch.setenv("ROBOTHOR_AI_NAME", "Jarvis")
        # owner.yaml is written under the HOME of the account running init;
        # pin it so a test can never touch the developer's own identity file.
        monkeypatch.setenv("HOME", str(tmp_path))

        import robothor.setup as setup_mod

        monkeypatch.setattr(
            setup_mod.httpx,
            "get",
            MagicMock(side_effect=Exception("no network")),
        )

        args = SimpleNamespace(
            yes=True,
            docker=False,
            skip_models=True,
            skip_db=True,
            workspace=str(workspace),
        )
        rc = run_init(args)
        assert rc == 0

        content = (workspace / ".env").read_text()
        assert "ROBOTHOR_OWNER_NAME" not in content
        assert "ROBOTHOR_AI_NAME=Jarvis" in content

        # ...and the identity it was given is in owner.yaml, which is what
        # load_owner_config() reads.
        from robothor.owner_config import load_owner_config

        owner = load_owner_config(tmp_path / ".robothor" / "owner.yaml")
        assert owner is not None
        assert owner.first_name == "Alice"


class TestDetectInstallMode:
    def test_checkout_when_template_dir_is_repo_templates(self, monkeypatch, tmp_path):
        import robothor.setup as setup_mod

        checkout_templates = tmp_path / "templates"
        checkout_templates.mkdir()
        monkeypatch.setattr(setup_mod, "_find_template_dir", lambda: checkout_templates)
        assert setup_mod._detect_install_mode() == "checkout"

    def test_wheel_when_template_dir_is_bundled_scaffold(self, monkeypatch, tmp_path):
        import robothor.setup as setup_mod

        bundled = tmp_path / "robothor" / "templates" / "bundled_scaffold"
        bundled.mkdir(parents=True)
        monkeypatch.setattr(setup_mod, "_find_template_dir", lambda: bundled)
        assert setup_mod._detect_install_mode() == "wheel"

    def test_wheel_when_template_dir_unresolved(self, monkeypatch):
        """No env override, no checkout marker, no bundled scaffold found."""
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_find_template_dir", lambda: None)
        assert setup_mod._detect_install_mode() == "wheel"


class TestPrintNextSteps:
    def test_wheel_mode_has_no_systemd_guidance(self, capsys):
        from robothor.setup import _print_next_steps

        _print_next_steps("wheel")
        out = capsys.readouterr().out

        assert "sudo systemctl" not in out
        assert "robothor serve" in out
        assert "[api]" in out  # cmd_serve errors without uvicorn/fastapi
        assert "robothor engine start" in out
        assert "robothor status" in out
        assert "robothor tui" in out
        assert "[tui]" in out
        assert "robothor agent install --preset standard" in out

    def test_checkout_mode_keeps_systemd_note(self, capsys):
        from robothor.setup import _print_next_steps

        _print_next_steps("checkout")
        out = capsys.readouterr().out

        assert "sudo systemctl start robothor-engine" in out
        assert "infra" in out.lower()  # note that the units need the repo's infra setup

    def test_claude_code_is_optional_not_step_one(self, capsys):
        from robothor.setup import _print_next_steps

        _print_next_steps("wheel")
        out = capsys.readouterr().out

        assert "Claude Code" in out
        assert "optional" in out.lower()
        numbered_lines = [line for line in out.splitlines() if line.strip()[:1].isdigit()]
        assert not any("Claude Code" in line for line in numbered_lines)


class TestRunInitNextStepsByMode:
    def test_wheel_mode_end_to_end_has_no_sudo_systemctl(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True,
            docker=False,
            skip_models=True,
            skip_db=True,
            workspace=str(workspace),
        )
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod.httpx, "get", MagicMock(side_effect=Exception("no network")))
        monkeypatch.setattr(setup_mod, "_detect_install_mode", lambda: "wheel")

        rc = run_init(args)
        assert rc == 0

        out = capsys.readouterr().out
        assert "sudo systemctl" not in out
        assert "robothor serve" in out

    def test_checkout_mode_end_to_end_keeps_systemd_note(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True,
            docker=False,
            skip_models=True,
            skip_db=True,
            workspace=str(workspace),
        )
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod.httpx, "get", MagicMock(side_effect=Exception("no network")))
        monkeypatch.setattr(setup_mod, "_detect_install_mode", lambda: "checkout")

        rc = run_init(args)
        assert rc == 0

        out = capsys.readouterr().out
        assert "sudo systemctl start robothor-engine" in out


class TestCliInit:
    def test_init_help(self, capsys):
        """robothor init --help should show all flags."""
        from robothor.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["init", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--yes" in out
        assert "--docker" in out
        assert "--skip-models" in out
        assert "--skip-db" in out
        assert "--workspace" in out

    def test_init_dispatches(self, monkeypatch):
        """robothor init should dispatch to run_init."""
        mock_run = MagicMock(return_value=0)
        monkeypatch.setattr("robothor.setup.run_init", mock_run)

        from robothor.cli import main

        rc = main(["init", "--yes", "--skip-models", "--skip-db"])
        assert rc == 0
        assert mock_run.called


class TestGenerationModelDefaultAgreement:
    """One default, everywhere.

    ROBOTHOR_GENERATION_MODEL's fallback default disagreed across four files
    (nemotron-3-super, qwen3:32b, qwen3-next:latest, qwen3:8b), so which model
    you got depended on which code path asked — and none of the four was
    guaranteed to exist on the box, because setup.sh only PRINTED the pull
    command for it. The first instance ran five months with a watchdog whose
    configured primary model did not exist locally.
    """

    CANONICAL = "qwen3:8b"

    def test_config_py_defaults(self, monkeypatch):
        """Both the dataclass field default AND the from_env fallback — they
        were two DIFFERENT wrong values (qwen3:32b and nemotron-3-super)."""
        monkeypatch.delenv("ROBOTHOR_GENERATION_MODEL", raising=False)
        assert OllamaConfig().generation_model == self.CANONICAL
        source = (REPO_ROOT / "robothor" / "config.py").read_text()
        assert f'os.environ.get("ROBOTHOR_GENERATION_MODEL", "{self.CANONICAL}")' in source

    def test_llm_ollama_default_source(self):
        text = (REPO_ROOT / "robothor" / "llm" / "ollama.py").read_text()
        assert f'os.environ.get("ROBOTHOR_GENERATION_MODEL", "{self.CANONICAL}")' in text

    def test_env_template_default(self):
        text = (REPO_ROOT / "infra" / "robothor.env.example").read_text()
        assert f"ROBOTHOR_GENERATION_MODEL={self.CANONICAL}" in text

    def test_docs_default(self):
        text = (REPO_ROOT / "docs" / "configuration.md").read_text()
        assert f"`ROBOTHOR_GENERATION_MODEL` | `{self.CANONICAL}`" in text

    def test_setup_actually_pulls_the_generation_model(self):
        """A print-only 'pull it yourself' note is how a manifest's model
        fails to exist for five months."""
        text = (REPO_ROOT / "infra" / "setup.sh").read_text()
        assert '"${ROBOTHOR_GENERATION_MODEL:-' + self.CANONICAL + '}"' in text
        assert "Pull it now with" not in text


class TestOwnerConfigFile:
    """`genus init` writes the operator identity where the platform reads it."""

    def test_writes_owner_yaml_that_load_owner_config_accepts(self, tmp_path):
        from robothor.owner_config import load_owner_config, write_owner_config

        path = tmp_path / ".robothor" / "owner.yaml"
        assert write_owner_config("Alice Example", "alice@example.com", path=path) is True

        owner = load_owner_config(path)
        assert owner is not None
        assert owner.first_name == "Alice"
        assert owner.last_name == "Example"
        assert owner.email == "alice@example.com"

    def test_does_not_overwrite_an_existing_identity(self, tmp_path):
        """A re-run of init must not rename the operator."""
        from robothor.owner_config import load_owner_config, write_owner_config

        path = tmp_path / ".robothor" / "owner.yaml"
        write_owner_config("Alice Example", "alice@example.com", path=path)
        assert write_owner_config("Bob Other", "bob@example.com", path=path) is False

        owner = load_owner_config(path)
        assert owner is not None and owner.email == "alice@example.com"

    def test_incomplete_identity_writes_nothing(self, tmp_path):
        """No name or no email is not half an identity; it is none."""
        from robothor.owner_config import write_owner_config

        path = tmp_path / ".robothor" / "owner.yaml"
        assert write_owner_config("", "alice@example.com", path=path) is False
        assert write_owner_config("Alice Example", "", path=path) is False
        assert not path.exists()
