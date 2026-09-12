"""The steps of the `local` substrate, each held to what it promises.

These are the steps that touch the machine, so the seams matter: the migrator,
the preset installer, the doctor and the HTTP client are all constructor
arguments, and the tests replace them wholesale. Nothing here starts a
service, dials a provider or migrates a database.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.doctor.context import HttpResponse
from robothor.init.context import InitContext
from robothor.init.steps import (
    AgentsStep,
    ChannelsStep,
    DatabaseStep,
    MigrateStep,
    ModelsStep,
    PrereqsStep,
    ProviderStep,
    SecretsStep,
    StepError,
    SubstrateStep,
    VerifyStep,
)
from robothor.init.substrate import AVAILABLE_SUBSTRATES, build_plan, get_substrate
from robothor.init.substrates.local import (
    LocalLinkStep,
    LocalServicesStep,
    LocalSignInStep,
    LocalSubstrate,
)


def _ctx(tmp_path, **kwargs: Any) -> InitContext:
    kwargs.setdefault("yes", True)
    return InitContext(workspace=tmp_path / "workspace", **kwargs)


class TestTheStepOrderIsTheSpecOrder:
    def test_local_runs_the_seventeen_steps_in_order(self):
        ids = [step.id for step in LocalSubstrate().steps()]

        assert ids == [
            "ack",
            "substrate",
            "prereqs",
            "detect",
            "provider",
            "identity",
            "workspace",
            "database",
            "migrate",
            "models",
            "agents",
            "channels",
            "secrets",
            "signin",
            "services",
            "verify",
            "link",
        ]

    def test_local_and_compose_are_offered_in_this_release(self):
        assert AVAILABLE_SUBSTRATES == ("local", "compose")

    @pytest.mark.parametrize("name", ["systemd", "helm"])
    def test_the_later_substrates_are_seams_that_say_where_they_land(self, name):
        with pytest.raises(NotImplementedError) as exc:
            get_substrate(name)

        assert "A10/A11/A12" in str(exc.value)

    def test_an_unknown_substrate_names_the_ones_that_exist(self):
        with pytest.raises(ValueError) as exc:
            get_substrate("kubernetes")

        assert "local" in str(exc.value)

    def test_build_plan_uses_the_context_substrate(self, tmp_path):
        plan = build_plan(_ctx(tmp_path))

        assert plan.substrate_name == "local"
        assert plan.steps[0].id == "ack"


class TestPrereqsArePerSubstrate:
    def test_local_requires_postgres_and_redis(self, tmp_path):
        asked: dict[str, Any] = {}

        def fake_prereqs(**kwargs: Any) -> list[dict[str, Any]]:
            asked.update(kwargs)
            return [
                {"name": "Python", "found": True, "detail": "3.12", "required": True, "hint": ""},
                {
                    "name": "PostgreSQL (psql)",
                    "found": True,
                    "detail": "found",
                    "required": True,
                    "hint": "",
                },
            ]

        step = PrereqsStep(
            required=("PostgreSQL (psql)", "Redis (redis-cli)"), prereqs=fake_prereqs
        )
        result = step.check(_ctx(tmp_path))

        assert result.ok is True
        assert asked["required"] == ("PostgreSQL (psql)", "Redis (redis-cli)")

    def test_a_missing_required_prereq_blocks_with_its_install_hint(self, tmp_path):
        def fake_prereqs(**kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "name": "Redis (redis-cli)",
                    "found": False,
                    "detail": "not found",
                    "required": True,
                    "hint": "apt install redis-server",
                }
            ]

        step = PrereqsStep(required=("Redis (redis-cli)",), prereqs=fake_prereqs)
        result = step.check(_ctx(tmp_path))

        assert result.ok is False
        assert "Redis" in result.detail
        assert "apt install redis-server" in result.fix_hint

    def test_an_optional_prereq_that_is_absent_does_not_block(self, tmp_path):
        def fake_prereqs(**kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "name": "Ollama",
                    "found": False,
                    "detail": "not reachable",
                    "required": False,
                    "hint": "",
                }
            ]

        step = PrereqsStep(required=(), prereqs=fake_prereqs)

        assert step.check(_ctx(tmp_path)).ok is True


class TestSubstrateStep:
    def test_an_unavailable_substrate_blocks_before_anything_is_written(self, tmp_path):
        result = SubstrateStep().check(_ctx(tmp_path, substrate_name="helm"))

        assert result.ok is False
        assert "local" in result.fix_hint


class TestMigrateUsesTheCanonicalMigrator:
    def test_apply_calls_db_migrate_apply_with_a_connection(self, tmp_path):
        seen: dict[str, Any] = {}
        connection = object()

        def fake_migrator(*, connection: Any) -> None:
            seen["connection"] = connection

        ctx = _ctx(tmp_path, db_factory=lambda: connection)
        MigrateStep(migrator=fake_migrator).apply(ctx)

        assert seen["connection"] is connection

    def test_a_migration_refusal_becomes_a_step_error_with_its_own_remedy(self, tmp_path):
        from robothor.db.migrate import MigrationError

        def fake_migrator(*, connection: Any) -> None:
            raise MigrationError("run with --adopt-baseline 001_init")

        ctx = _ctx(tmp_path, db_factory=lambda: object())

        with pytest.raises(StepError) as exc:
            MigrateStep(migrator=fake_migrator).apply(ctx)

        assert "--adopt-baseline 001_init" in str(exc.value)

    def test_a_safety_refusal_reaches_the_operator_intact(self, tmp_path):
        """The migrator names its own remedy; flattening it sends the operator
        hunting a network fault that does not exist."""
        from robothor.db.migrate import BASELINE_UNADOPTED_MESSAGE, MigrationHistoryError

        def fake_migrator(*, connection: Any) -> None:
            raise MigrationHistoryError(BASELINE_UNADOPTED_MESSAGE)

        ctx = _ctx(tmp_path, db_factory=lambda: object())

        with pytest.raises(StepError) as exc:
            MigrateStep(migrator=fake_migrator).apply(ctx)

        message = str(exc.value)
        assert "--adopt-baseline" in message
        assert "connection" not in message.lower().replace("connection settings", "")

    def test_the_connection_is_closed_even_when_the_migrator_raises(self, tmp_path):
        closed: list[str] = []

        class _Connection:
            def close(self) -> None:
                closed.append("closed")

        def fake_migrator(*, connection: Any) -> None:
            raise RuntimeError("boom")

        ctx = _ctx(tmp_path, db_factory=_Connection)

        with pytest.raises(RuntimeError):
            MigrateStep(migrator=fake_migrator).apply(ctx)

        assert closed == ["closed"]

    def test_skip_db_skips_the_step_rather_than_failing_it(self, tmp_path):
        ctx = _ctx(tmp_path, answers={"skip_db": True})

        assert MigrateStep().check(ctx).action == "skip"


class TestDatabaseStep:
    def test_an_unreachable_database_blocks_with_a_fix_hint(self, tmp_path):
        def boom() -> Any:
            raise RuntimeError("could not connect to server")

        result = DatabaseStep().check(_ctx(tmp_path, db_factory=boom))

        assert result.ok is False
        assert "could not connect" in result.detail
        assert "ROBOTHOR_DB_" in result.fix_hint


class TestAgentsPreset:
    def test_the_installer_is_called_with_the_chosen_preset(self, tmp_path):
        seen: dict[str, Any] = {}

        def fake_install(preset: str, **kwargs: Any) -> dict[str, Any]:
            seen["preset"] = preset
            return {
                "unknown_preset": False,
                "available": ["minimal", "standard", "full"],
                "requested": 3,
                "installed": ["main", "scout", "scribe"],
                "failed": {},
                "missing": [],
            }

        ctx = _ctx(tmp_path, answers={"preset": "full"})
        AgentsStep(installer=fake_install).apply(ctx)

        assert seen["preset"] == "full"
        assert "3" in ctx.details["agents"]

    def test_an_unknown_preset_names_the_ones_that_exist(self, tmp_path):
        def fake_install(preset: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "unknown_preset": True,
                "available": ["minimal", "standard", "full"],
                "requested": 0,
                "installed": [],
                "failed": {},
                "missing": [],
            }

        ctx = _ctx(tmp_path, answers={"preset": "enormous"})

        with pytest.raises(StepError) as exc:
            AgentsStep(installer=fake_install).apply(ctx)

        assert "minimal" in str(exc.value)

    def test_a_partly_failed_install_reports_which_agents_failed(self, tmp_path):
        def fake_install(preset: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "unknown_preset": False,
                "available": ["standard"],
                "requested": 2,
                "installed": ["main"],
                "failed": {"scout": "ValueError: bad template"},
                "missing": [],
            }

        ctx = _ctx(tmp_path, answers={"preset": "standard"})
        AgentsStep(installer=fake_install).apply(ctx)

        assert "scout" in ctx.details["agents"]


class TestModelsFollowOllama:
    """The pull tracks whether Ollama is THERE, not which model answers chat.

    (See `test_failure_paths.py` for the cloud-provider case and the reason.)
    The HTTP seam is injected rather than left to resolve: a unit test that
    reaches the developer's own Ollama passes or fails with the host.
    """

    @staticmethod
    def _reachable(tmp_path, **answers):
        def fetch(method, url, body, timeout):
            return HttpResponse(status=200, body='{"models": []}')

        return _ctx(tmp_path, answers=answers, http_fetch=fetch)

    def test_a_reachable_ollama_pulls_the_required_models(self, tmp_path):
        pulled: list[list[str]] = []
        ctx = self._reachable(tmp_path, provider_id="ollama")
        step = ModelsStep(puller=lambda base, models: pulled.append(list(models)))

        assert step.check(ctx).action == "create"
        step.apply(ctx)
        assert pulled and "qwen3-embedding:0.6b" in pulled[0]

    def test_skip_models_wins_over_a_reachable_ollama(self, tmp_path):
        ctx = self._reachable(tmp_path, provider_id="ollama", skip_models=True)

        assert ModelsStep().check(ctx).action == "skip"


class TestChannelsAreOptional:
    def test_no_token_means_the_step_is_skipped_not_failed(self, tmp_path):
        result = ChannelsStep().check(_ctx(tmp_path))

        assert result.ok is True
        assert result.action == "skip"

    def test_a_token_is_verified_with_get_me(self, tmp_path):
        seen: list[str] = []

        def fetch(method, url, body, timeout):
            seen.append(url)
            return HttpResponse(status=200, body='{"ok": true, "result": {"username": "a_bot"}}')

        ctx = _ctx(tmp_path, answers={"telegram_token": "123:abc"}, http_fetch=fetch)
        result = ChannelsStep().check(ctx)

        assert result.ok is True
        assert "a_bot" in result.detail
        assert seen and seen[0].endswith("/getMe")

    def test_a_rejected_token_warns_and_never_reaches_a_report(self, tmp_path):
        def fetch(method, url, body, timeout):
            return HttpResponse(status=401, body='{"ok": false, "description": "Unauthorized"}')

        ctx = _ctx(tmp_path, answers={"telegram_token": "123:abc"}, http_fetch=fetch)
        result = ChannelsStep().check(ctx)

        assert result.ok is False
        assert "123:abc" not in result.detail
        assert "123:abc" not in result.fix_hint

    def test_offline_skips_the_probe_rather_than_failing_it(self, tmp_path):
        def fetch(method, url, body, timeout):  # pragma: no cover - must not run
            raise AssertionError("offline must not leave the box")

        ctx = _ctx(tmp_path, answers={"telegram_token": "123:abc"}, http_fetch=fetch, offline=True)

        assert ChannelsStep().check(ctx).action == "skip"


class TestSecretsBackend:
    def test_local_defaults_to_the_env_backend(self, tmp_path):
        ctx = _ctx(tmp_path)
        result = SecretsStep().check(ctx)

        assert result.ok is True
        assert "env" in result.detail

    def test_an_unknown_backend_blocks_with_the_three_that_exist(self, tmp_path):
        ctx = _ctx(tmp_path, answers={"secrets_backend": "kms"})
        result = SecretsStep().check(ctx)

        assert result.ok is False
        assert "sops" in result.fix_hint


class TestVerifyIsTheGate:
    def test_zero_required_failures_passes(self, tmp_path):
        from robothor.doctor.runner import CheckResult as DoctorResult
        from robothor.doctor.runner import DoctorReport

        report = DoctorReport(
            results=[
                DoctorResult(
                    "db.migrations", "Migrations", "database", "required", "pass", "", False
                ),
                DoctorResult(
                    "channels.slack", "Slack", "channels", "recommended", "fail", "no token", False
                ),
            ]
        )
        ctx = _ctx(tmp_path)
        VerifyStep(doctor=lambda ctx_: report).apply(ctx)

        assert "0 required" in ctx.details["verify"]

    def test_a_required_failure_names_the_check(self, tmp_path):
        from robothor.doctor.runner import CheckResult as DoctorResult
        from robothor.doctor.runner import DoctorReport

        report = DoctorReport(
            results=[
                DoctorResult(
                    "db.rbac_service_role",
                    "Service role",
                    "database",
                    "required",
                    "fail",
                    "role not seeded",
                    True,
                )
            ]
        )

        with pytest.raises(StepError) as exc:
            VerifyStep(doctor=lambda ctx_: report).apply(_ctx(tmp_path))

        assert "db.rbac_service_role" in str(exc.value)
        assert "role not seeded" in str(exc.value)

    def test_it_stays_offline_unless_the_provider_was_probed(self, tmp_path):
        seen: dict[str, Any] = {}

        def fake_doctor(doctor_ctx: Any) -> Any:
            from robothor.doctor.runner import DoctorReport

            seen["offline"] = doctor_ctx.offline
            return DoctorReport(results=[])

        VerifyStep(doctor=fake_doctor).apply(_ctx(tmp_path))
        assert seen["offline"] is True

        VerifyStep(doctor=fake_doctor).apply(_ctx(tmp_path, answers={"provider_probed": True}))
        assert seen["offline"] is False

    def test_it_tells_the_doctor_whether_anything_was_asked_to_start(self, tmp_path):
        """Without `--start` the wizard launches no daemons, so requiring them
        would fail the install on a state it deliberately did not create."""
        seen: dict[str, Any] = {}

        def fake_doctor(doctor_ctx: Any) -> Any:
            from robothor.doctor.runner import DoctorReport

            seen["services_expected"] = doctor_ctx.services_expected
            return DoctorReport(results=[])

        VerifyStep(doctor=fake_doctor).apply(_ctx(tmp_path))
        assert seen["services_expected"] is False

        VerifyStep(doctor=fake_doctor).apply(_ctx(tmp_path, answers={"start": True}))
        assert seen["services_expected"] is True


class TestServicesStep:
    def test_without_start_it_prints_the_two_commands_and_starts_nothing(self, tmp_path, capsys):
        ctx = _ctx(tmp_path)
        LocalServicesStep(starter=lambda: pytest.fail("must not start")).apply(ctx)

        printed = capsys.readouterr().out
        assert "engine" in printed
        assert "bridge" in printed

    def test_start_runs_the_existing_start_command(self, tmp_path):
        started: list[str] = []
        ctx = _ctx(tmp_path, answers={"start": True})

        LocalServicesStep(starter=lambda: started.append("started")).apply(ctx)

        assert started == ["started"]


class TestTheFirstRunLink:
    def test_the_url_is_a_single_use_setup_link(self, tmp_path, capsys):
        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True)

        LocalLinkStep(is_a_terminal=lambda: True).apply(ctx)

        assert "/setup?token=" in ctx.first_run_url
        printed = capsys.readouterr().out
        assert "/setup?token=" in printed

    def test_a_headless_box_also_gets_the_port_forward_line(self, tmp_path, capsys):
        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True)

        LocalLinkStep(is_a_terminal=lambda: False).apply(ctx)

        printed = capsys.readouterr().out
        assert "ssh -L" in printed

    def test_a_terminal_gets_no_port_forward_noise(self, tmp_path, capsys):
        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True)

        LocalLinkStep(is_a_terminal=lambda: True).apply(ctx)

        assert "ssh -L" not in capsys.readouterr().out

    def test_a_link_that_cannot_be_minted_does_not_fail_the_install(self, tmp_path):
        """Ten minutes of work must not be thrown away over a token file."""

        class _NoLink(LocalSubstrate):
            def first_run_url(self, ctx: InitContext) -> str:
                raise OSError("read-only file system")

        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True)

        LocalLinkStep(substrate=_NoLink(), is_a_terminal=lambda: True).apply(ctx)

        assert ctx.first_run_url == ""
        assert "genus auth setup-link" in ctx.details["link"]


class TestProviderStep:
    def test_no_credential_and_no_local_model_blocks_the_run(self, tmp_path, monkeypatch):
        # Stubbed rather than left to resolve: this box has real provider keys,
        # and a check that consults them passes or fails with the host.
        monkeypatch.setattr("robothor.engine.key_pool.scan_slots", lambda provider_id: [])
        ctx = _ctx(tmp_path)
        result = ProviderStep().check(ctx)

        assert result.ok is False
        assert "OPENROUTER_API_KEY" in result.fix_hint

    def test_a_failed_probe_blocks_rather_than_writing_a_provider(self, tmp_path, monkeypatch):
        from robothor.engine.key_pool import ResolvedKey
        from robothor.init.provider_probe import ProbeResult

        monkeypatch.setattr(
            "robothor.engine.key_pool.scan_slots",
            lambda provider_id: [ResolvedKey(position=1, key="sk-bad", source="env")],
        )
        ctx = _ctx(
            tmp_path,
            answers={
                "provider_id": "openrouter",
                "provider_model": "openrouter/openai/gpt-5.4",
            },
        )
        step = ProviderStep(
            probe=lambda *a, **k: ProbeResult(False, "openrouter", "m", "401 Unauthorized", True)
        )

        with pytest.raises(StepError) as exc:
            step.apply(ctx)

        assert "401 Unauthorized" in str(exc.value)
        assert "sk-bad" not in str(exc.value)

    def test_offline_records_the_choice_without_dialling(self, tmp_path):
        def never(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError("offline must not dial a provider")

        ctx = _ctx(
            tmp_path,
            offline=True,
            answers={"provider_id": "openrouter", "provider_model": "openrouter/openai/gpt-5.4"},
        )
        ctx.workspace.mkdir(parents=True)
        ProviderStep(probe=never).apply(ctx)

        assert ctx.answers["provider_probed"] is False
        assert "--offline" in ctx.details["provider"]


class TestTheLocalInstallCanBeSignedIntoAtAll:
    """`secrets.bridge_sso` is a REQUIRED doctor check, and only the compose
    substrate minted the secret it asks for. So a documented local install
    failed its own verify step on a value nothing on that path ever wrote --
    and, had it not, the bridge would have refused every SSO exchange with the
    dashboard showing a sign-in page that could not work.
    """

    def test_it_writes_both_shared_secrets_and_turns_local_login_on(self, tmp_path):
        from robothor.secrets.env_file import instance_env_path, parse_env_file

        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        LocalSignInStep().apply(ctx)

        values = parse_env_file(instance_env_path(ctx.workspace).read_text(encoding="utf-8"))
        assert len(values["AUTH_SECRET"]) >= 32
        assert len(values["GENUS_BRIDGE_SSO_SECRET"]) >= 32
        assert values["AUTH_SECRET"] != values["GENUS_BRIDGE_SSO_SECRET"]
        assert values["GENUS_LOCAL_LOGIN"] == "true"

    def test_the_file_is_readable_only_by_its_owner(self, tmp_path):
        import stat

        from robothor.secrets.env_file import instance_env_path

        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        LocalSignInStep().apply(ctx)

        mode = instance_env_path(ctx.workspace).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_a_re_run_does_not_rotate_them(self, tmp_path):
        """Rotating AUTH_SECRET signs every session out; rotating the SSO
        secret leaves the bridge and the dashboard disagreeing until both
        restart. A re-run of `genus init` must do neither."""
        from robothor.secrets.env_file import instance_env_path, parse_env_file

        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        LocalSignInStep().apply(ctx)
        first = parse_env_file(instance_env_path(ctx.workspace).read_text(encoding="utf-8"))

        LocalSignInStep().apply(_ctx(tmp_path))
        second = parse_env_file(instance_env_path(ctx.workspace).read_text(encoding="utf-8"))

        assert second["AUTH_SECRET"] == first["AUTH_SECRET"]
        assert second["GENUS_BRIDGE_SSO_SECRET"] == first["GENUS_BRIDGE_SSO_SECRET"]

    def test_it_runs_before_verify_reads_the_file(self):
        ids = [step.id for step in LocalSubstrate().steps()]

        assert ids.index("signin") < ids.index("verify")


class TestTheWizardDialsOllamaWhereItActuallyIs:
    """`ollama.url` is EMPTY by default -- it "falls back to host and port", as
    its own description says -- and three wizard sites read it raw. So the
    model pull POSTed to a relative "/api/pull", `pull_ollama_models` swallowed
    the failure, and the step reported "pulled qwen3-embedding:0.6b" on an
    instance that had pulled nothing and would have no embeddings.
    """

    def test_the_models_step_falls_back_to_host_and_port(self, tmp_path):
        pulled: dict[str, Any] = {}
        ctx = _ctx(tmp_path, http_fetch=lambda *_a, **_k: HttpResponse(status=200))

        ModelsStep(puller=lambda base, models: pulled.update(base=base)).apply(ctx)

        assert pulled["base"] == "http://127.0.0.1:11434"

    def test_an_explicit_url_still_wins(self, tmp_path, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://ollama.example.test:11434/")
        reset_settings()
        pulled: dict[str, Any] = {}
        ctx = _ctx(tmp_path, http_fetch=lambda *_a, **_k: HttpResponse(status=200))

        ModelsStep(puller=lambda base, models: pulled.update(base=base)).apply(ctx)

        assert pulled["base"] == "http://ollama.example.test:11434"

    def test_the_settings_resolver_is_the_one_the_doctor_already_used(self, monkeypatch):
        from robothor.settings import get_settings, reset_settings

        reset_settings()
        assert get_settings().ollama.base_url == "http://127.0.0.1:11434"

        monkeypatch.setenv("ROBOTHOR_OLLAMA_HOST", "box.example.test")
        monkeypatch.setenv("ROBOTHOR_OLLAMA_PORT", "1234")
        reset_settings()
        assert get_settings().ollama.base_url == "http://box.example.test:1234"
