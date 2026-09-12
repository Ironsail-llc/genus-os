"""Tests for robothor.setup — the `genus init` orchestrator.

The steps themselves are tested in ``robothor/init/tests``; what this module
holds is the orchestration (two phases, the streams, the exit code) and the
handful of helpers ``robothor.setup`` still owns outright.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from robothor.config import DatabaseConfig, OllamaConfig, RedisConfig
from robothor.init.steps import BaseStep, CheckResult
from robothor.setup import (
    REQUIRED_MODELS,
    check_prerequisites,
    create_workspace,
    generate_docker_compose,
    pull_ollama_models,
    run_init,
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

    def test_a_substrate_can_make_postgres_and_redis_required(self, monkeypatch):
        """Every prerequisite used to be optional on every substrate, so a box
        with no PostgreSQL passed this check and failed at the migration."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        results = check_prerequisites(required=("PostgreSQL (psql)", "Redis (redis-cli)"))

        by_name = {row["name"]: row for row in results}
        assert by_name["PostgreSQL (psql)"]["required"] is True
        assert by_name["Redis (redis-cli)"]["required"] is True
        assert by_name["Ollama"]["required"] is False

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


class _FakeSubstrate:
    """A substrate whose steps are whatever a test hands it."""

    name = "local"

    def __init__(self, steps):
        self._steps = steps

    def steps(self):
        return self._steps

    def first_run_url(self, ctx):
        return "http://127.0.0.1:3004/setup?token=REDACTED"


def _use_steps(monkeypatch, *steps):
    monkeypatch.setattr(
        "robothor.init.substrate.get_substrate",
        lambda name: _FakeSubstrate(list(steps)),
    )


def _init_args(workspace, **overrides):
    args = SimpleNamespace(
        yes=True,
        docker=False,
        skip_models=True,
        skip_db=True,
        workspace=str(workspace),
        substrate=None,
        dry_run=False,
        json=False,
        offline=True,
        preset=None,
        provider=None,
        model=None,
        secrets_backend=None,
        telegram_token=None,
        owner_name=None,
        owner_email=None,
        start=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class _Touch(BaseStep):
    """A step whose only effect is a file, so "did it write" is answerable."""

    def __init__(self, step_id, *, ok=True, required=True):
        self.id = step_id
        self.title = step_id.title()
        self.required = required
        self._ok = ok

    def check(self, ctx):
        return CheckResult(self._ok, detail=f"{self.id} detail", fix_hint="do the thing")

    def apply(self, ctx):
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        (ctx.workspace / f"{self.id}.marker").write_text("x", encoding="utf-8")


class TestRunInitIsTwoPhase:
    def test_a_failing_required_check_writes_nothing_and_exits_one(
        self, tmp_path, capsys, monkeypatch
    ):
        workspace = tmp_path / "workspace"
        _use_steps(monkeypatch, _Touch("workspace"), _Touch("database", ok=False))

        rc = run_init(_init_args(workspace))

        assert rc == 1
        assert not workspace.exists()
        out = capsys.readouterr().out
        assert "Nothing was written" in out
        assert "database" in out

    def test_a_clean_run_applies_every_step(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "workspace"
        _use_steps(monkeypatch, _Touch("workspace"), _Touch("agents"))

        rc = run_init(_init_args(workspace))

        assert rc == 0
        assert (workspace / "workspace.marker").exists()
        assert (workspace / "agents.marker").exists()
        assert "Genus OS is initialized" in capsys.readouterr().out

    def test_dry_run_prints_the_plan_and_writes_nothing(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "workspace"
        _use_steps(monkeypatch, _Touch("workspace"))

        rc = run_init(_init_args(workspace, dry_run=True))

        assert rc == 0
        assert not workspace.exists()
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "workspace detail" in out

    def test_json_mode_puts_one_document_on_stdout_and_humans_on_stderr(
        self, tmp_path, capsys, monkeypatch
    ):
        workspace = tmp_path / "workspace"
        _use_steps(monkeypatch, _Touch("workspace"))

        rc = run_init(_init_args(workspace, json=True))

        assert rc == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert set(payload) == {"plan", "steps", "first_run_url", "exit_code"}
        assert payload["steps"][0]["id"] == "workspace"
        # The human narration is still readable, just not on the stream the
        # JSON consumer is parsing.
        assert "Genus OS Setup" in captured.err
        assert "Genus OS Setup" not in captured.out

    def test_a_substrate_that_is_not_built_yet_says_where_it_lands(self, tmp_path, capsys):
        rc = run_init(_init_args(tmp_path / "workspace", substrate="systemd"))

        assert rc == 1
        assert "A10/A11/A12" in capsys.readouterr().out

    def test_an_unknown_substrate_names_the_ones_that_exist(self, tmp_path, capsys):
        rc = run_init(_init_args(tmp_path / "workspace", substrate="kubernetes"))

        assert rc == 1
        assert "local" in capsys.readouterr().out


class TestWorkspacePointer:
    def test_it_carries_only_what_config_yaml_cannot_hold(self, tmp_path):
        from robothor.setup import write_workspace_pointer

        path = write_workspace_pointer(tmp_path)

        assert path == tmp_path / ".env"
        content = path.read_text()
        assert f"ROBOTHOR_WORKSPACE={tmp_path}" in content
        # Everything else now lives in config.yaml, written by the step that
        # owns it. Two files holding one value is how `genus config set`
        # reports "applied" while the service reads the old number.
        for duplicated in (
            "ROBOTHOR_DB_HOST",
            "ROBOTHOR_REDIS_HOST",
            "ROBOTHOR_OLLAMA_HOST",
            "ROBOTHOR_AI_NAME",
            "ROBOTHOR_OWNER_NAME",
            "ROBOTHOR_OWNER_EMAIL",
        ):
            assert duplicated not in content

    def test_an_existing_file_is_never_overwritten(self, tmp_path):
        from robothor.setup import write_workspace_pointer

        (tmp_path / ".env").write_text("EXISTING=value\n")

        assert write_workspace_pointer(tmp_path) is None
        assert (tmp_path / ".env").read_text() == "EXISTING=value\n"


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


class TestServiceCommandsByMode:
    """The two commands init prints must be runnable on THIS install.

    A wheel has no systemd units, so telling it to `systemctl start` something
    is advice that cannot work; a checkout has them, and its in-process
    equivalents would start a second copy beside the units.
    """

    def test_wheel_mode_has_no_systemd_guidance(self, monkeypatch):
        from robothor.init.substrates.local import LocalServicesStep

        monkeypatch.setattr("robothor.setup._detect_install_mode", lambda: "wheel")
        commands = LocalServicesStep.commands()

        assert not any("systemctl" in command for command in commands)
        assert any("genus engine start" in command for command in commands)
        assert any("genus serve" in command for command in commands)

    def test_checkout_mode_uses_the_units(self, monkeypatch):
        from robothor.init.substrates.local import LocalServicesStep

        monkeypatch.setattr("robothor.setup._detect_install_mode", lambda: "checkout")
        commands = LocalServicesStep.commands()

        assert any("systemctl start robothor-engine" in command for command in commands)
        assert any("robothor-bridge" in command for command in commands)

    def test_both_modes_name_the_engine_and_the_bridge(self, monkeypatch):
        from robothor.init.substrates.local import LocalServicesStep

        for mode in ("wheel", "checkout"):
            monkeypatch.setattr("robothor.setup._detect_install_mode", lambda mode=mode: mode)
            printed = " ".join(LocalServicesStep.commands())
            assert "engine" in printed
            assert "bridge" in printed


class TestCliInit:
    def test_init_help(self, capsys):
        """robothor init --help should show all flags."""
        from robothor.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["init", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        for flag in (
            "--yes",
            "--docker",
            "--skip-models",
            "--skip-db",
            "--workspace",
            "--substrate",
            "--dry-run",
            "--json",
            "--offline",
            "--preset",
            "--provider",
            "--model",
            "--secrets-backend",
            "--telegram-token",
            "--owner-name",
            "--owner-email",
            "--start",
        ):
            assert flag in out, f"{flag} is not in `genus init --help`"

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
