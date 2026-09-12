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

        assert "ROBOTHOR_DB_PASSWORD=generated-by-init" in body
        assert "ROBOTHOR_DB_NAME=robothor_memory" in body
        assert "ROBOTHOR_LAST_RESORT_MODEL=openrouter/openai/gpt-5.4" in body
        assert "ROBOTHOR_SECRETS_BACKEND=env" in body
        assert "GENUS_IMAGE_TAG=v" in body

    def test_it_is_readable_only_by_its_owner(self, tmp_path):
        _, path = self._render(tmp_path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_provider_key_is_in_that_file_and_in_no_other(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "robothor.init.provider_probe.resolve_provider_key", lambda _id: PROVIDER_KEY
        )
        ctx, path = self._render(tmp_path)

        assert f"OPENROUTER_API_KEY={PROVIDER_KEY}" in path.read_text(encoding="utf-8")
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
        assert f"GENUS_WORKSPACE={tmp_path / 'instance'}" in body
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


class TestWaitPollsUntilTheStackAnswers:
    def test_it_reports_every_service_it_found_green(self, tmp_path, capsys):
        ctx = _ctx(tmp_path, http_fetch=_all_ready)
        ctx.stream = None

        ComposeWaitStep(sleep=lambda _seconds: None).apply(ctx)

        assert ctx.report["compose"]["ready"] == {
            "engine": True,
            "bridge": True,
            "orchestrator": True,
            "dashboard": True,
        }
        assert "engine" in capsys.readouterr().out

    def test_it_polls_the_ports_the_settings_model_declares(self, tmp_path):
        asked: list[str] = []

        def fetch(method, url, body, timeout):
            asked.append(url)
            return HttpResponse(status=200)

        ComposeWaitStep(sleep=lambda _s: None).apply(_ctx(tmp_path, http_fetch=fetch))

        assert "http://127.0.0.1:18800/ready" in asked
        assert "http://127.0.0.1:9100/ready" in asked
        assert "http://127.0.0.1:9099/ready" in asked
        assert "http://127.0.0.1:3004/api/ready" in asked

    def test_a_service_that_never_answers_fails_the_step_by_name(self, tmp_path):
        def fetch(method, url, body, timeout):
            if "9100" in url:
                return HttpResponse(status=0, error="ConnectError")
            return HttpResponse(status=200)

        ctx = _ctx(tmp_path, http_fetch=fetch, answers={"wait_timeout": 6})

        with pytest.raises(StepError) as exc:
            ComposeWaitStep(sleep=lambda _s: None).apply(ctx)

        assert "bridge" in str(exc.value)
        assert "docker compose logs" in str(exc.value)
        assert ctx.report["compose"]["ready"]["bridge"] is False
        assert ctx.report["compose"]["ready"]["engine"] is True

    def test_it_stops_polling_the_moment_everything_is_green(self, tmp_path):
        slept: list[float] = []
        ctx = _ctx(tmp_path, http_fetch=_all_ready)

        ComposeWaitStep(sleep=slept.append).apply(ctx)

        assert slept == []


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
