"""Three promises the flags make, and the stream contract behind `--json`.

`--docker` exists for a fresh box with no database, and phase 1's database
check ran before phase 2 started the containers — so the one flag whose whole
purpose is "you do not have PostgreSQL yet" refused to run without PostgreSQL.

`--json` promises one document on stdout. Several helpers the steps call use
`print()`, so an Ollama pull, a `docker compose up` or a missing scaffold
template put plain text in front of the JSON and no parser could read it.

And the settings the old `.env` held have to land in config.yaml, which is the
file something actually reads, rather than being dropped on the way.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import yaml

from robothor.doctor.context import HttpResponse
from robothor.init.context import InitContext
from robothor.init.steps import BaseStep, CheckResult, DatabaseStep, PrereqsStep, WorkspaceStep


def _ctx(tmp_path, **kwargs: Any) -> InitContext:
    kwargs.setdefault("yes", True)
    return InitContext(workspace=tmp_path / "workspace", **kwargs)


def _settings(tmp_path) -> dict[str, Any]:
    path = tmp_path / "workspace" / ".robothor" / "config.yaml"
    if not path.exists():
        return {}
    document = yaml.safe_load(path.read_text()) or {}
    return dict(document.get("settings") or {})


class TestDockerProvidesTheDatabaseItIsAskedFor:
    def test_the_database_check_waits_for_the_containers(self, tmp_path):
        """With --docker, phase 1 cannot connect yet and must not pretend to."""

        def dead() -> Any:
            raise RuntimeError("connection refused")

        ctx = _ctx(tmp_path, answers={"docker": True}, db_factory=dead)
        result = DatabaseStep().check(ctx)

        assert result.ok is True
        assert "docker" in result.detail.lower()

    def test_a_socket_connection_reads_as_one_not_as_an_empty_row(self, tmp_path):
        target = DatabaseStep._target(
            {"user": "", "host": "", "port": 5432, "dbname": "robothor_memory"}
        )

        assert target == "(this account)@(unix socket):5432/robothor_memory"

    def test_without_docker_an_unreachable_database_still_blocks(self, tmp_path):
        def dead() -> Any:
            raise RuntimeError("connection refused")

        ctx = _ctx(tmp_path, db_factory=dead)

        assert DatabaseStep().check(ctx).ok is False

    def test_apply_proves_the_connection_that_check_could_not(self, tmp_path):
        from robothor.init.steps import StepError

        def dead() -> Any:
            raise RuntimeError("connection refused")

        ctx = _ctx(tmp_path, answers={"docker": True}, db_factory=dead)

        try:
            DatabaseStep().apply(ctx)
        except StepError as exc:
            assert "connection refused" in str(exc)
        else:  # pragma: no cover - the point of the test
            raise AssertionError("apply accepted a database it could not reach")

    def test_the_generated_compose_gets_a_real_password(self, tmp_path):
        from robothor.setup import build_init_context

        args = SimpleNamespace(
            yes=True,
            docker=True,
            workspace=str(tmp_path / "workspace"),
            skip_models=True,
            skip_db=False,
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
        ctx = build_init_context(args)

        assert ctx.answers["db_password"], "--docker generated an empty POSTGRES_PASSWORD"
        assert len(str(ctx.answers["db_password"])) >= 16

    def test_the_compose_and_the_config_agree_about_the_connection(self, tmp_path):
        """A compose file saying one thing and config.yaml another is an
        instance that starts a database it cannot then reach."""
        from robothor.setup import DOCKER_COMPOSE_TEMPLATE, build_init_context

        args = SimpleNamespace(
            yes=True, docker=True, workspace=str(tmp_path / "workspace"), start=False
        )
        ctx = build_init_context(args)
        config = ctx.db_config()

        assert config["host"] == "127.0.0.1"
        assert config["port"] == 5432
        assert f"POSTGRES_USER: {config['user']}" in DOCKER_COMPOSE_TEMPLATE
        assert f"POSTGRES_DB: {config['dbname']}" in DOCKER_COMPOSE_TEMPLATE

    def test_the_compose_file_is_not_world_readable(self, tmp_path):
        """It carries POSTGRES_PASSWORD in plaintext, and with --docker that is
        a credential this wizard minted."""
        from robothor.setup import generate_docker_compose

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        path = generate_docker_compose(workspace, "generated-password")

        assert path.stat().st_mode & 0o077 == 0
        assert "generated-password" in path.read_text()

    def test_the_container_wait_uses_the_generated_password(self, tmp_path, monkeypatch):
        import robothor.setup as setup_mod

        seen: dict[str, Any] = {}

        def fake_connect(**kwargs: Any) -> Any:
            seen.update(kwargs)
            raise RuntimeError("not yet")

        monkeypatch.setattr(
            "robothor.setup._compose_up", lambda workspace, compose_file: (True, "")
        )
        monkeypatch.setitem(
            __import__("sys").modules, "psycopg2", SimpleNamespace(connect=fake_connect)
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        setup_mod.generate_docker_compose(workspace, "generated-password")
        setup_mod.wait_for_services(workspace, timeout=0, password="generated-password")

        assert seen.get("password") == "generated-password"


class TestJsonStdoutCarriesOnlyTheDocument:
    def test_a_step_that_prints_does_not_corrupt_the_payload(self, tmp_path, capsys, monkeypatch):
        from robothor.setup import run_init

        class _Noisy(BaseStep):
            id = "models"
            title = "Local models"

            def check(self, ctx: InitContext) -> CheckResult:
                return CheckResult(True, detail="will pull")

            def apply(self, ctx: InitContext) -> None:
                # Exactly what pull_ollama_models, wait_for_services and the
                # scaffold warnings do today.
                print("  Pulling qwen3-embedding:0.6b ... failed (Connection refused)")

        class _Substrate:
            name = "local"

            def steps(self):
                return (_Noisy(),)

            def first_run_url(self, ctx: InitContext) -> str:
                return ""

        monkeypatch.setattr("robothor.init.substrate.get_substrate", lambda name: _Substrate())

        args = SimpleNamespace(
            yes=True,
            json=True,
            dry_run=False,
            workspace=str(tmp_path / "workspace"),
            substrate=None,
            offline=True,
            docker=False,
            skip_models=False,
            skip_db=True,
            preset=None,
            provider=None,
            model=None,
            secrets_backend=None,
            telegram_token=None,
            owner_name=None,
            owner_email=None,
            start=False,
        )
        rc = run_init(args)

        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["steps"][0]["id"] == "models"
        assert "Pulling" not in captured.out
        assert "Pulling" in captured.err


class TestTheSettingsTheEnvFileUsedToHold:
    def test_the_workspace_step_records_the_ai_identity_and_timezone(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            answers={
                "ai_name": "Jarvis",
                "ai_email": "jarvis@example.com",
                "timezone": "Europe/Lisbon",
            },
        )
        WorkspaceStep().apply(ctx)

        settings = _settings(tmp_path)
        assert settings["channels"]["ai_name"] == "Jarvis"
        assert settings["channels"]["ai_email"] == "jarvis@example.com"
        assert settings["engine"]["timezone"] == "Europe/Lisbon"

    def test_the_workspace_step_records_the_redis_and_ollama_endpoints(self, tmp_path):
        """Not the database step: Redis and Ollama have nothing to do with the
        database, and putting them there meant `--skip-db` dropped both."""
        ctx = _ctx(
            tmp_path,
            answers={
                "redis_host": "redis.internal.test",
                "redis_port": 6380,
                "ollama_host": "gpu.internal.test",
                "ollama_port": 11435,
            },
        )
        WorkspaceStep().apply(ctx)

        settings = _settings(tmp_path)
        assert settings["redis"]["host"] == "redis.internal.test"
        assert settings["redis"]["port"] == 6380
        assert settings["ollama"]["host"] == "gpu.internal.test"
        assert settings["ollama"]["port"] == 11435

    def test_skip_db_keeps_the_endpoints_it_has_nothing_to_do_with(self, tmp_path):
        from robothor.init.plan import InitPlan, run_plan
        from robothor.init.steps import DatabaseStep as _Db

        ctx = _ctx(
            tmp_path,
            answers={"skip_db": True, "redis_host": "redis.internal.test"},
        )
        run_plan(ctx, InitPlan("local", [WorkspaceStep(), _Db()]))

        settings = _settings(tmp_path)
        assert settings["redis"]["host"] == "redis.internal.test"
        assert "database" not in settings

    def test_a_timezone_from_the_environment_reaches_the_answers(self, tmp_path, monkeypatch):
        from robothor.setup import build_init_context

        monkeypatch.setenv("ROBOTHOR_TIMEZONE", "Europe/Lisbon")
        ctx = build_init_context(
            SimpleNamespace(yes=True, workspace=str(tmp_path / "workspace"), docker=False)
        )

        assert ctx.answers["timezone"] == "Europe/Lisbon"

    def test_the_redis_endpoint_an_operator_exported_is_persisted(self, tmp_path, monkeypatch):
        """An instance that depends forever on a variable being in the unit
        environment is an instance `genus config get redis.host` lies about."""
        from robothor.setup import build_init_context

        monkeypatch.setenv("ROBOTHOR_REDIS_HOST", "redis.internal.test")
        ctx = build_init_context(
            SimpleNamespace(yes=True, workspace=str(tmp_path / "workspace"), docker=False)
        )

        assert ctx.answers["redis_host"] == "redis.internal.test"


class TestPrereqsProvisionsBeforeTheDatabaseIsChecked:
    def test_the_docker_step_runs_at_prereqs_which_is_before_database(self):
        from robothor.init.substrates.local import LocalSubstrate

        ids = [step.id for step in LocalSubstrate().steps()]

        assert ids.index("prereqs") < ids.index("database")

    def test_prereqs_generates_the_compose_file_with_the_password(self, tmp_path, monkeypatch):
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod, "wait_for_services", lambda *a, **k: True)
        ctx = _ctx(tmp_path, answers={"docker": True, "db_password": "generated-password"})
        ctx.workspace.mkdir(parents=True)

        PrereqsStep(required=(), prereqs=lambda **k: []).apply(ctx)

        compose = (ctx.workspace / "docker-compose.yml").read_text()
        assert 'POSTGRES_PASSWORD: "generated-password"' in compose


class TestOllamaProbeSeam:
    def test_the_models_check_uses_the_context_http_seam(self, tmp_path):
        """So a unit test never depends on the developer's own Ollama."""
        from robothor.init.steps import ModelsStep

        calls: list[str] = []

        def fetch(method, url, body, timeout):
            calls.append(url)
            return HttpResponse(status=200, body='{"models": []}')

        ModelsStep().check(_ctx(tmp_path, http_fetch=fetch))

        assert calls and calls[0].endswith("/api/tags")
