"""The compose substrate: four steps that touch Docker, and none that guess.

Every ``docker`` call goes through the context's ``runner`` seam and every
``/ready`` poll through its ``http`` seam, so nothing here starts a container,
pulls an image or waits a real second. That is not only about speed: this test
module runs on the machine that hosts the appliance, and a test that shelled
out to a real ``docker compose up`` would restart the operator's instance.

The workspace containment fixture (``robothor/init/tests/conftest.py``) pins
``HOME`` and ``ROBOTHOR_WORKSPACE`` at a throwaway directory and checks a
sentinel manifest afterwards, so a step that resolved a workspace from the
environment instead of from the context it was handed fails the run.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from robothor.doctor.context import HttpResponse
from robothor.init.context import CommandResult, InitContext
from robothor.init.steps import StepError
from robothor.init.substrate import AVAILABLE_SUBSTRATES, get_substrate
from robothor.init.substrates.compose import (
    APPS_FILE,
    BASE_FILE,
    GPU_FILE,
    MINIMUM_DOCKER_MAJOR,
    ComposePrereqsStep,
    ComposeRenderStep,
    ComposeSubstrate,
    ComposeUpStep,
    ComposeWaitStep,
    compose_files,
    env_file_path,
)

PROVIDER_KEY = "sk-test-not-a-real-key-00000000"

#: The dashboard half of the platform, for the readiness contract below.
APP = Path(__file__).resolve().parents[3] / "app" / "src" / "lib"


def dashboard_readiness_keys() -> tuple[str, ...]:
    """Every environment variable the dashboard's `/api/ready` demands.

    Derived from `health.ts` and `auth-local.ts` rather than copied here. A
    hardcoded list is a list that goes stale silently, and the failure it hides
    is the one this test exists for: a `wait` step that can never go green
    because nothing wrote a secret the dashboard requires.
    """
    health = (APP / "services" / "health.ts").read_text(encoding="utf-8")
    common = re.search(r"const common = \[(.*?)\]", health, re.DOTALL)
    assert common, "health.ts no longer declares `const common = [...]`; re-derive this list"
    names = tuple(re.findall(r'"([A-Z0-9_]+)"', common.group(1)))
    assert names, "no variable names in health.ts's `common` array"

    local = (APP / "auth-local.ts").read_text(encoding="utf-8")
    sign_in = re.search(r"process\.env\.([A-Z0-9_]+)", local)
    assert sign_in, "auth-local.ts no longer reads a sign-in variable"
    return (*names, sign_in.group(1))


class FakeDocker:
    """A docker CLI as the wizard sees it: versions, a GPU, and a recorder."""

    def __init__(
        self,
        *,
        docker: str | None = "Docker version 29.2.1, build a5c7197",
        compose: str | None = "Docker Compose version v2.39.1",
        nvidia: bool = False,
        up_code: int = 0,
    ) -> None:
        self.docker = docker
        self.compose = compose
        self.nvidia = nvidia
        self.up_code = up_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> CommandResult:
        self.calls.append(list(argv))
        if argv[0] == "nvidia-smi":
            if not self.nvidia:
                return CommandResult(127, err="nvidia-smi: command not found")
            return CommandResult(0, out="NVIDIA-SMI 580.65.06")
        if argv[:2] == ["docker", "--version"]:
            if self.docker is None:
                return CommandResult(127, err="docker: command not found")
            return CommandResult(0, out=self.docker)
        if argv[:3] == ["docker", "compose", "version"]:
            if self.compose is None:
                return CommandResult(1, err="docker: 'compose' is not a docker command")
            return CommandResult(0, out=self.compose)
        if "up" in argv:
            return CommandResult(self.up_code, err="" if not self.up_code else "no space left")
        return CommandResult(0)

    @property
    def up_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if "up" in argv]


def _compose_dir(root: Path, *, gpu: bool = True) -> Path:
    """A directory holding the release compose files, as a fresh box has it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / BASE_FILE).write_text("services: {}\n", encoding="utf-8")
    (root / APPS_FILE).write_text("services: {}\n", encoding="utf-8")
    if gpu:
        (root / GPU_FILE).write_text("services: {}\n", encoding="utf-8")
    return root


def _ctx(tmp_path: Path, *, docker: FakeDocker | None = None, **kwargs: Any) -> InitContext:
    workspace = kwargs.pop("workspace", tmp_path / "instance")
    answers: dict[str, Any] = {
        "provider_id": "openrouter",
        "provider_model": "openrouter/openai/gpt-5.4",
        "db_host": "127.0.0.1",
        "db_port": 5432,
        "db_name": "robothor_memory",
        "db_user": "robothor",
        "db_password": "generated-by-init",
        "owner_name": "Ada Lovelace",
        "owner_email": "ada@example.com",
    }
    answers.update(kwargs.pop("answers", {}))
    _compose_dir(Path(workspace))
    return InitContext(
        workspace=workspace,
        answers=answers,
        yes=True,
        substrate_name="compose",
        runner=(docker or FakeDocker()),
        **kwargs,
    )


def _all_ready(_method: str, url: str, _body: Any, _timeout: float) -> HttpResponse:
    return HttpResponse(status=200, body="ready")


class FakeTime:
    """A clock the fake sleep advances.

    The wait step's deadline is wall clock, so a test that stubs `sleep` alone
    would spin against the real one for the whole timeout — `--wait-timeout 180`
    would be a three-minute test.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


class TestTheStepsAreTheSubstrateContract:
    def test_compose_is_selectable(self):
        assert "compose" in AVAILABLE_SUBSTRATES
        assert isinstance(get_substrate("compose"), ComposeSubstrate)

    def test_the_plan_adds_the_docker_steps_and_drops_the_migrate_one(self):
        ids = [step.id for step in ComposeSubstrate().steps()]

        assert ids == [
            "ack",
            "substrate",
            "prereqs",
            "detect",
            "provider",
            "identity",
            "workspace",
            "agents",
            "render",
            "up",
            "wait",
            "models",
            "database",
            "operator",
            "channels",
            "secrets",
            "verify",
            "link",
        ]

    def test_nothing_migrates_from_the_wizard(self):
        # The compose `migrate` service owns the schema, and the three Python
        # services wait on it with service_completed_successfully. A wizard step
        # doing it too would race the container that everything else gates on.
        assert "migrate" not in [step.id for step in ComposeSubstrate().steps()]

    def test_phase_one_never_dials_a_database_the_stack_has_not_started(self, tmp_path):
        def explode() -> Any:
            raise AssertionError("phase 1 connected to a database `up` has not started yet")

        ctx = _ctx(tmp_path, db_factory=explode)
        step = next(item for item in ComposeSubstrate().steps() if item.id == "database")

        result = step.check(ctx)

        assert result.ok
        assert "compose stack" in result.detail

    def test_the_first_run_url_lands_in_the_browser_wizard(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True, exist_ok=True)

        url = ComposeSubstrate().first_run_url(ctx)

        # The token itself is the credential; asserting on its VALUE would put
        # a live one in this file's output on every failure.
        assert url.startswith("http://127.0.0.1:3004/setup?token=")
        assert len(url.split("token=")[1]) > 20


class TestPrereqsRefuseABoxDockerCannotRunOn:
    def test_it_passes_on_a_box_with_docker_and_compose_v2(self, tmp_path):
        docker = FakeDocker()
        result = ComposePrereqsStep().check(_ctx(tmp_path, docker=docker))

        assert result.ok
        assert "29.2.1" in result.detail

    def test_no_docker_at_all_blocks_the_run(self, tmp_path):
        result = ComposePrereqsStep().check(_ctx(tmp_path, docker=FakeDocker(docker=None)))

        assert not result.ok
        assert "docker" in result.fix_hint.lower()

    def test_an_ancient_docker_blocks_the_run(self, tmp_path):
        old = FakeDocker(docker="Docker version 20.10.24, build 297e128")

        result = ComposePrereqsStep().check(_ctx(tmp_path, docker=old))

        assert not result.ok
        assert str(MINIMUM_DOCKER_MAJOR) in result.detail + result.fix_hint

    def test_compose_v1_or_no_plugin_blocks_the_run(self, tmp_path):
        result = ComposePrereqsStep().check(_ctx(tmp_path, docker=FakeDocker(compose=None)))

        assert not result.ok
        assert "compose" in (result.detail + result.fix_hint).lower()

    def test_a_missing_gpu_is_not_a_failure(self, tmp_path):
        ctx = _ctx(tmp_path, docker=FakeDocker(nvidia=False))

        result = ComposePrereqsStep().check(ctx)

        assert result.ok
        assert ctx.answers["gpu"] is False

    def test_a_gpu_is_recorded_for_the_overlay(self, tmp_path):
        ctx = _ctx(tmp_path, docker=FakeDocker(nvidia=True))

        assert ComposePrereqsStep().check(ctx).ok
        assert ctx.answers["gpu"] is True

    def test_a_root_install_is_refused_before_anything_is_written(self, tmp_path, monkeypatch):
        """Root would give the containers root, or files root owns.

        The compose services run as the installing account (the workspace is a
        bind mount of 0600 files), so a root install either runs every container
        as root or leaves a workspace uid 1000 cannot read. Phase 1 is where
        that has to be said -- after `render` the identity is already written.
        """
        monkeypatch.setattr(os, "getuid", lambda: 0)

        result = ComposePrereqsStep().check(_ctx(tmp_path))

        assert not result.ok
        assert "root" in (result.detail + result.fix_hint).lower()

    def test_missing_compose_files_block_with_the_commands_that_fetch_them(self, tmp_path):
        empty = tmp_path / "no-compose-files"
        empty.mkdir()
        ctx = _ctx(tmp_path, answers={"compose_dir": str(empty)})

        result = ComposePrereqsStep().check(ctx)

        assert not result.ok
        assert APPS_FILE in result.fix_hint
        assert "curl" in result.fix_hint

    def test_a_named_directory_is_the_only_one_consulted(self, tmp_path):
        """An operator who names a directory gets that one's files or none.

        Falling through to the checkout's ``infra/`` would run a stack the
        operator did not choose, and say nothing about it.
        """
        empty = tmp_path / "named-but-empty"
        empty.mkdir()

        assert compose_files(_ctx(tmp_path, answers={"compose_dir": str(empty)})) == []


class TestTheOverlayListIsChosenNotAssumed:
    def test_every_path_it_hands_docker_is_absolute(self, tmp_path, monkeypatch):
        """``--workspace .`` is a documented way to run this substrate.

        Compose resolves a relative ``-f`` or ``--env-file`` against ITS project
        directory, not the operator's shell, so a relative path here means
        something different to every later `docker compose` command.
        """
        ctx = _ctx(tmp_path)
        monkeypatch.chdir(ctx.workspace)
        ctx.workspace = Path()

        assert env_file_path(ctx).is_absolute()
        assert [path.is_absolute() for path in compose_files(ctx)] == [True, True]

    def test_the_base_and_release_files_are_always_used(self, tmp_path):
        ctx = _ctx(tmp_path)

        names = [path.name for path in compose_files(ctx)]

        assert names == [BASE_FILE, APPS_FILE]

    def test_the_gpu_overlay_is_added_only_when_nvidia_smi_answers(self, tmp_path):
        with_gpu = _ctx(tmp_path, answers={"gpu": True})
        without = _ctx(tmp_path / "other", answers={"gpu": False})

        assert [path.name for path in compose_files(with_gpu)][-1] == GPU_FILE
        assert GPU_FILE not in [path.name for path in compose_files(without)]

    def test_a_gpu_box_missing_the_overlay_file_still_runs(self, tmp_path):
        ctx = _ctx(tmp_path, answers={"gpu": True})
        (ctx.workspace / GPU_FILE).unlink()

        assert [path.name for path in compose_files(ctx)] == [BASE_FILE, APPS_FILE]


class TestRenderWritesTheEnvFileAndNothingElseReadable:
    def _render(self, tmp_path: Path, **kwargs: Any) -> tuple[InitContext, Path]:
        ctx = _ctx(tmp_path, **kwargs)
        ComposePrereqsStep().check(ctx)
        ComposeRenderStep().apply(ctx)
        return ctx, env_file_path(ctx)

    def test_the_env_file_carries_the_answers(self, tmp_path):
        _, path = self._render(tmp_path)
        body = path.read_text(encoding="utf-8")

        assert 'ROBOTHOR_DB_PASSWORD="generated-by-init"' in body
        assert 'ROBOTHOR_DB_NAME="robothor_memory"' in body
        assert 'ROBOTHOR_LAST_RESORT_MODEL="openrouter/openai/gpt-5.4"' in body
        assert 'ROBOTHOR_SECRETS_BACKEND="env"' in body
        assert 'GENUS_IMAGE_TAG="v' in body

    def test_a_password_with_a_space_survives_both_readers(self, tmp_path):
        """Two parsers read this file, and an unquoted space breaks one.

        `docker compose` takes the rest of the line; the `set -a; . ./genus.env`
        the quickstart tells an operator to run is a shell, which would export
        the first word and treat the rest as a command.
        """
        _, path = self._render(tmp_path, answers={"db_password": 'two words "and" a quote'})

        line = next(
            row
            for row in path.read_text(encoding="utf-8").splitlines()
            if row.startswith("ROBOTHOR_DB_PASSWORD=")
        )

        assert line == 'ROBOTHOR_DB_PASSWORD="two words \\"and\\" a quote"'

    def test_it_is_readable_only_by_its_owner(self, tmp_path):
        _, path = self._render(tmp_path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_provider_key_is_in_that_file_and_in_no_other(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "robothor.init.provider_probe.resolve_provider_key", lambda _id: PROVIDER_KEY
        )
        ctx, path = self._render(tmp_path)

        assert f'OPENROUTER_API_KEY="{PROVIDER_KEY}"' in path.read_text(encoding="utf-8")
        leaked = [
            other
            for other in ctx.workspace.rglob("*")
            if other.is_file()
            and other != path
            and PROVIDER_KEY in other.read_text(encoding="utf-8", errors="ignore")
        ]
        assert leaked == [], f"the provider key was also written to {leaked}"
        # Nor into anything the plan, the JSON or init_state.yaml will render.
        assert PROVIDER_KEY not in repr(ctx.answers)
        assert PROVIDER_KEY not in repr(ctx.report)

    def test_the_container_workspace_is_never_the_host_one(self, tmp_path):
        _, path = self._render(tmp_path)
        body = path.read_text(encoding="utf-8")

        # GENUS_WORKSPACE is the host path compose bind-mounts; ROBOTHOR_WORKSPACE
        # is /workspace INSIDE the container and is set by the compose file. A
        # host path leaking into the env file points every container at a
        # directory that does not exist in it.
        assert f'GENUS_WORKSPACE="{(tmp_path / "instance").resolve()}"' in body
        assert "ROBOTHOR_WORKSPACE=" not in body

    def test_the_identity_travels_into_the_workspace_the_containers_mount(
        self, tmp_path, monkeypatch
    ):
        owner = tmp_path / "home" / ".robothor" / "owner.yaml"
        owner.parent.mkdir(parents=True)
        owner.write_text("email: ada@example.com\ntenant_id: default\n", encoding="utf-8")
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))

        ctx, _ = self._render(tmp_path)
        copied = ctx.workspace / ".robothor" / "owner.yaml"

        # ~/.robothor is NOT mounted into the containers: one workspace mount is
        # the whole contract, so the identity has to be inside it.
        assert copied.exists()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600

    def test_the_dashboard_is_given_what_it_needs_to_become_ready(self, tmp_path):
        """`/api/ready` 503s until AUTH_SECRET, GENUS_BRIDGE_SSO_SECRET and one
        sign-in method are in the dashboard's environment.

        Nothing wrote them, so `wait` timed out on every fresh box, the `link`
        step never ran, and the operator never got a /setup URL from an install
        that had otherwise succeeded. The keys are derived from the dashboard's
        own source, so this test fails if that contract moves.
        """
        _, path = self._render(tmp_path)
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )

        for name in dashboard_readiness_keys():
            assert name in values, f"genus.env carries no {name}; /api/ready can never pass"
            assert values[name].strip('"'), f"{name} is empty"

    def test_the_shared_secrets_are_real_secrets(self, tmp_path):
        _, path = self._render(tmp_path)
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        auth = values["AUTH_SECRET"].strip('"')
        sso = values["GENUS_BRIDGE_SSO_SECRET"].strip('"')

        assert len(auth) >= 32
        assert len(sso) >= 32
        assert auth != sso

    def test_a_re_run_does_not_rotate_them(self, tmp_path):
        """`render` is not resumable -- it runs on every `genus init`.

        Minting a new AUTH_SECRET each time would sign every existing session
        out, and a new SSO secret would leave the dashboard and the bridge
        disagreeing until both restarted.
        """
        ctx, path = self._render(tmp_path)
        first = path.read_text(encoding="utf-8")

        ComposeRenderStep().apply(ctx)

        assert path.read_text(encoding="utf-8") == first

    def test_the_containers_run_as_the_account_that_installed(self, tmp_path, monkeypatch):
        """The workspace is a bind mount full of 0600 files owned by this uid.

        The image's own user is uid 1000; on a box whose operator is not 1000
        the bridge cannot read setup_token.yaml -- the only door into a fresh
        instance -- so the compose file takes the uid from here.
        """
        monkeypatch.setattr(os, "getuid", lambda: 4242)
        monkeypatch.setattr(os, "getgid", lambda: 4343)

        _, path = self._render(tmp_path)
        body = path.read_text(encoding="utf-8")

        assert 'GENUS_UID="4242"' in body
        assert 'GENUS_GID="4343"' in body

    def test_it_reports_the_files_and_images_it_chose(self, tmp_path):
        ctx, _ = self._render(tmp_path)
        payload = ctx.report["compose"]

        assert [Path(name).name for name in payload["files"]] == [BASE_FILE, APPS_FILE]
        assert payload["images"]["python"].startswith("ghcr.io/ironsail-llc/genus-os/python:")
        assert payload["images"]["app"].startswith("ghcr.io/ironsail-llc/genus-os/app:")


class TestUpRunsOneCommandAndSaysWhichOne:
    def test_it_passes_every_chosen_file_and_the_env_file(self, tmp_path):
        docker = FakeDocker(nvidia=True)
        ctx = _ctx(tmp_path, docker=docker, answers={"gpu": True})
        ComposeRenderStep().apply(ctx)

        ComposeUpStep().apply(ctx)

        argv = docker.up_calls[0]
        assert argv[:2] == ["docker", "compose"]
        assert argv[-2:] == ["up", "-d"]
        assert str(env_file_path(ctx)) in argv
        assert argv.count("-f") == 3

    def test_a_failed_up_is_a_step_failure_naming_docker(self, tmp_path):
        docker = FakeDocker(up_code=1)
        ctx = _ctx(tmp_path, docker=docker)
        ComposeRenderStep().apply(ctx)

        with pytest.raises(StepError) as exc:
            ComposeUpStep().apply(ctx)

        assert "no space left" in str(exc.value)

    def test_a_dry_run_would_create_the_containers_without_starting_them(self, tmp_path):
        ctx = _ctx(tmp_path, dry_run=True)

        assert "--no-start" in ComposeUpStep().command(ctx)
        assert "--no-start" not in ComposeUpStep().command(_ctx(tmp_path / "wet"))


@pytest.fixture
def _clock() -> FakeTime:
    return FakeTime()


class TestWaitPollsUntilTheStackAnswers:
    def test_it_reports_every_service_it_found_green(self, tmp_path, capsys, _clock):
        ctx = _ctx(tmp_path, http_fetch=_all_ready)
        ctx.stream = None

        ComposeWaitStep(sleep=_clock.sleep, clock=_clock.clock).apply(ctx)

        assert ctx.report["compose"]["ready"] == {
            "engine": True,
            "bridge": True,
            "orchestrator": True,
            "dashboard": True,
        }
        assert "engine" in capsys.readouterr().out

    def test_it_polls_the_ports_the_settings_model_declares(self, tmp_path, _clock):
        asked: list[str] = []

        def fetch(method, url, body, timeout):
            asked.append(url)
            return HttpResponse(status=200)

        ComposeWaitStep(sleep=_clock.sleep, clock=_clock.clock).apply(
            _ctx(tmp_path, http_fetch=fetch)
        )

        assert "http://127.0.0.1:18800/ready" in asked
        assert "http://127.0.0.1:9100/ready" in asked
        assert "http://127.0.0.1:9099/ready" in asked
        assert "http://127.0.0.1:3004/api/ready" in asked

    def test_a_service_that_never_answers_fails_the_step_by_name(self, tmp_path, _clock):
        def fetch(method, url, body, timeout):
            if "9100" in url:
                return HttpResponse(status=0, error="ConnectError")
            return HttpResponse(status=200)

        ctx = _ctx(tmp_path, http_fetch=fetch, answers={"wait_timeout": 6})

        with pytest.raises(StepError) as exc:
            ComposeWaitStep(sleep=_clock.sleep, clock=_clock.clock).apply(ctx)

        assert "bridge" in str(exc.value)
        assert "docker logs robothor-bridge" in str(exc.value)
        assert ctx.report["compose"]["ready"]["bridge"] is False
        assert ctx.report["compose"]["ready"]["engine"] is True

    def test_the_timeout_is_wall_clock_not_a_poll_budget(self, tmp_path):
        """Each round makes four HTTP calls that can each block for seconds.

        Charging the timeout 3s per round while the round costs 20s of wall
        clock turned a documented `--wait-timeout 180` into a twenty-minute
        block against a stack whose ports accept and then hang.
        """
        now = [0.0]
        rounds: list[float] = []

        def fetch(method, url, body, timeout):
            # One round of four calls costs 20s of wall clock -- four probes
            # that connect and then hang for the context's 5s budget.
            now[0] += 5.0
            return HttpResponse(status=0, error="timed out")

        def clock() -> float:
            return now[0]

        ctx = _ctx(tmp_path, http_fetch=fetch, answers={"wait_timeout": 60})
        step = ComposeWaitStep(sleep=lambda seconds: rounds.append(seconds), clock=clock)

        with pytest.raises(StepError):
            step.apply(ctx)

        # 60s of budget at 20s a round is three rounds, not the twenty a poll
        # budget would have allowed.
        assert len(rounds) <= 3, f"{len(rounds)} rounds: the deadline is not wall clock"

    def test_a_404_from_something_else_on_that_port_is_not_readiness(self, tmp_path, _clock):
        def fetch(method, url, body, timeout):
            if "9100" in url:
                return HttpResponse(status=404, body="Not Found")
            return HttpResponse(status=200)

        ctx = _ctx(tmp_path, http_fetch=fetch, answers={"wait_timeout": 3})

        with pytest.raises(StepError) as exc:
            ComposeWaitStep(sleep=_clock.sleep, clock=_clock.clock).apply(ctx)

        assert "bridge" in str(exc.value)

    def test_an_enforcing_service_counts_as_up(self, tmp_path, _clock):
        # The bridge gates its health route behind auth in production, and a
        # service refusing an unauthenticated probe is a service that started.
        def fetch(method, url, body, timeout):
            return HttpResponse(status=401 if "9100" in url else 200)

        ComposeWaitStep(sleep=_clock.sleep, clock=_clock.clock).apply(
            _ctx(tmp_path, http_fetch=fetch)
        )

    def test_the_remediation_line_is_runnable_as_printed(self, tmp_path, _clock):
        """`docker compose logs bridge` is not: a bare `docker compose` in the
        workspace sees only the base file and answers "no such service"."""

        def fetch(method, url, body, timeout):
            return HttpResponse(status=0) if "9100" in url else HttpResponse(status=200)

        with pytest.raises(StepError) as exc:
            ComposeWaitStep(sleep=_clock.sleep, clock=_clock.clock).apply(
                _ctx(tmp_path, http_fetch=fetch, answers={"wait_timeout": 3})
            )

        assert "docker logs robothor-bridge" in str(exc.value)

    def test_it_stops_polling_the_moment_everything_is_green(self, tmp_path):
        slept: list[float] = []
        ctx = _ctx(tmp_path, http_fetch=_all_ready)

        ComposeWaitStep(sleep=slept.append).apply(ctx)

        assert slept == []


class TestVerifyCanReachTheDatabaseTheWizardCreated:
    """The generated database password exists in exactly one place.

    `DatabaseStep` connects from `ctx.answers`, but `VerifyStep` runs the doctor
    IN PROCESS and the doctor resolves the connection from the environment --
    where a password the wizard generated has never been. `db.connect` is a
    required check, so the run ended in exit 1 after the stack was up and the
    fleet was written.
    """

    def _verify(self, ctx) -> dict[str, str]:
        from robothor.doctor.runner import DoctorReport
        from robothor.init.steps import VerifyStep

        seen: dict[str, str] = {}

        def doctor(context, checks=None):
            seen["password"] = os.environ.get("ROBOTHOR_DB_PASSWORD", "")
            return DoctorReport(results=[])

        VerifyStep(doctor=doctor).apply(ctx)
        return seen

    def test_it_loads_the_env_file_the_render_step_wrote(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_DB_PASSWORD", raising=False)
        ctx = _ctx(tmp_path)
        ComposeRenderStep().apply(ctx)

        assert self._verify(ctx)["password"] == "generated-by-init"

    def test_a_readable_env_file_is_refused_and_said_out_loud(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("ROBOTHOR_DB_PASSWORD", raising=False)
        ctx = _ctx(tmp_path)
        ComposeRenderStep().apply(ctx)
        env_file_path(ctx).chmod(0o644)

        assert self._verify(ctx)["password"] == ""
        assert "chmod 600" in capsys.readouterr().out


class TestADryRunTouchesNothing:
    def test_no_file_is_written_and_no_container_is_created(self, tmp_path):
        from robothor.init.plan import run_plan
        from robothor.init.substrate import build_plan

        docker = FakeDocker()
        ctx = _ctx(tmp_path, docker=docker, dry_run=True, http_fetch=_all_ready)
        plan = build_plan(ctx)

        result = run_plan(ctx, plan)

        assert not env_file_path(ctx).exists()
        assert docker.up_calls == []
        assert [row.status for row in result.steps if row.id == "render"] == ["planned"]


class TestTheJsonCarriesWhatTheStackIs:
    def test_the_compose_payload_reaches_the_document(self, tmp_path):
        from robothor.init.plan import InitResult

        ctx = _ctx(tmp_path)
        ComposePrereqsStep().check(ctx)
        ComposeRenderStep().apply(ctx)

        payload = InitResult(extra=dict(ctx.report)).as_dict()

        assert set(payload) >= {"plan", "steps", "first_run_url", "exit_code", "compose"}
        assert "files" in payload["compose"]

    def test_a_local_install_gains_no_compose_key(self):
        from robothor.init.plan import InitResult

        assert set(InitResult().as_dict()) == {"plan", "steps", "first_run_url", "exit_code"}
