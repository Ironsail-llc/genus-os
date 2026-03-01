"""Tests for robothor.setup — init wizard."""

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
        assert "ROBOTHOR_OWNER_NAME=Alice" in content
        assert "ROBOTHOR_AI_NAME=Jarvis" in content
        assert "ROBOTHOR_DB_HOST=myhost" in content
        assert "ROBOTHOR_DB_PORT=5433" in content
        assert "ROBOTHOR_DB_NAME=mydb" in content
        assert "ROBOTHOR_DB_USER=me" in content
        assert "ROBOTHOR_DB_PASSWORD=pw" in content
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
            patch("robothor.cli._find_migration_sql", return_value="CREATE TABLE t (id int);"),
        ):
            db = DatabaseConfig(host="localhost", password="test")
            count = run_migration(db)

        assert count == 17
        assert mock_cur.execute.call_count == 2  # SQL + count query

    def test_connection_failure_returns_negative(self):
        """If DB is unreachable, should return -1."""
        with (
            patch("psycopg2.connect", side_effect=Exception("Connection refused")),
            patch("robothor.cli._find_migration_sql", return_value="CREATE TABLE t (id int);"),
        ):
            db = DatabaseConfig(host="nonexistent")
            count = run_migration(db)
        assert count == -1

    def test_no_migration_sql_returns_negative(self):
        """If migration SQL is missing, should return -1."""
        with patch("robothor.cli._find_migration_sql", return_value=None):
            db = DatabaseConfig()
            count = run_migration(db)
        assert count == -1


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
        assert "Setup complete!" in out

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
        monkeypatch.setenv("ROBOTHOR_AI_NAME", "Jarvis")

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
        assert "ROBOTHOR_OWNER_NAME=Alice" in content
        assert "ROBOTHOR_AI_NAME=Jarvis" in content


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
