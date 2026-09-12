"""Docker Compose on one node — the enterprise pilot substrate.

The path a pilot actually takes: a fresh Linux box, released images from GHCR,
one ``docker compose up``, and a browser on ``/setup``. What the wizard owns
here is everything that path cannot do for itself.

**The schema is not a wizard step.** ``infra/docker-compose.apps.yml`` carries
a one-shot ``migrate`` service that engine, bridge and orchestrator wait on
with ``service_completed_successfully``. Migrating from the host as well would
race the container that everything else already gates on, and would need the
host to hold a database credential it otherwise never uses. The wizard
confirms the outcome instead: ``verify`` runs the doctor, whose ``db.migrations``
check is ``required``.

**The prerequisites are the substrate's own.** Docker 24+ and the Compose v2
plugin are REQUIRED, so a box without them is refused in phase 1 with nothing
written. ``nvidia-smi`` is optional and decides one thing: whether the GPU
overlay joins the file list. A GPU reservation on a box with no NVIDIA runtime
fails the entire stack, which is why it is an overlay at all.

**Credentials live in one 0600 file.** ``genus.env`` is written by ``render``
and read by every container through ``env_file``. Nothing is inlined in the
compose file, and the provider key exists in exactly one place on disk.

**Nothing the wizard writes points a container at the host.** The env file
carries ``GENUS_WORKSPACE`` (a host path compose bind-mounts) and never
``ROBOTHOR_WORKSPACE`` (``/workspace``, inside the container). The compose file
sets the container-side names explicitly for the same reason.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.init.steps import (
    AckStep,
    AgentsStep,
    BaseStep,
    ChannelsStep,
    CheckResult,
    DatabaseStep,
    DetectStep,
    IdentityStep,
    ModelsStep,
    OperatorStep,
    ProviderStep,
    SecretsStep,
    StepError,
    SubstrateStep,
    VerifyStep,
    WorkspaceStep,
)
from robothor.init.substrates.local import LocalLinkStep

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.init.context import CommandResult, InitContext
    from robothor.init.steps import Step

__all__ = [
    "APPS_FILE",
    "BASE_FILE",
    "DEFAULT_WAIT_TIMEOUT_S",
    "GPU_FILE",
    "MINIMUM_DOCKER_MAJOR",
    "ComposeModelsStep",
    "ComposePrereqsStep",
    "ComposeRenderStep",
    "ComposeSubstrate",
    "ComposeUpStep",
    "ComposeWaitStep",
    "compose_directory",
    "compose_files",
    "env_file_path",
    "image_tag",
    "ready_endpoints",
    "substrate",
]

#: The stack, in the order ``docker compose -f`` must see it.
BASE_FILE = "docker-compose.yml"
APPS_FILE = "docker-compose.apps.yml"
GPU_FILE = "docker-compose.gpu.yml"

#: The file ``render`` writes and every container reads. Mode 0600.
ENV_FILENAME = "genus.env"

#: Compose v2 as a ``docker`` subcommand, ``depends_on`` conditions and
#: ``service_completed_successfully`` all landed well before this. Below it the
#: release file does not mean what it says.
MINIMUM_DOCKER_MAJOR = 24

#: Seconds ``wait`` gives the four services to answer ``/ready``. A first run
#: pulls two images and applies the whole migration manifest before the engine
#: even starts, so this is minutes, not seconds.
DEFAULT_WAIT_TIMEOUT_S = 180

#: Seconds between ``/ready`` polls.
READY_POLL_INTERVAL_S = 3.0

#: Seconds ``docker compose up -d`` may take. It pulls two images on a first
#: run, over whatever link the box has.
UP_TIMEOUT_S = 1800.0

#: Seconds a version probe may take.
PROBE_TIMEOUT_S = 15.0

#: Where the released images come from. There is no ``:latest`` — the build
#: publishes vX.Y.Z, vX.Y, vX and sha-<short> — so the tag is chosen here, from
#: the version of the CLI doing the installing, and written into the env file.
IMAGE_REPOSITORY = "ghcr.io/ironsail-llc/genus-os"


def _release_url(name: str) -> str:
    return f"https://raw.githubusercontent.com/Ironsail-llc/genus-os/main/infra/{name}"


#: What to do about a box with no compose files. Named commands rather than
#: "not found": the wheel does not carry these files, so on a `pip install`
#: box this is the NORMAL first failure, not a broken installation.
FETCH_HINT = (
    f"fetch the release files first: curl -fsSLO {_release_url(BASE_FILE)} && "
    f"curl -fsSLO {_release_url(APPS_FILE)} (run them in the workspace directory), "
    "or run genus init from a checkout"
)


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------


def compose_directory(ctx: InitContext) -> Path | None:
    """The directory holding the release compose files, or ``None``.

    Searched rather than assumed, because the three shapes an operator arrives
    in are all legitimate: the files curled into the workspace next to
    ``genus.env``, the files curled into whatever directory they are standing
    in, or a git checkout whose ``infra/`` already has them.

    An answered ``compose_dir`` is the ONLY candidate: an operator who names a
    directory and gets a different one's files is being told the opposite of
    what they asked for, and on this substrate that means running a stack they
    did not choose.
    """
    answered = str(ctx.answers.get("compose_dir") or "")
    if answered:
        candidates = [Path(answered).expanduser()]
    else:
        candidates = [
            ctx.workspace,
            Path.cwd(),
            Path.cwd() / "infra",
            # The checkout this module is running from, if it is one.
            Path(__file__).resolve().parents[3] / "infra",
        ]
    for candidate in candidates:
        if (candidate / BASE_FILE).is_file() and (candidate / APPS_FILE).is_file():
            return candidate
    return None


def compose_files(ctx: InitContext) -> list[Path]:
    """Every ``-f`` file, in order. Empty when the release files are missing.

    The GPU overlay joins only when ``nvidia-smi`` answered AND the file is
    there: a missing overlay on a GPU box costs some embedding throughput,
    while a reservation on a box with no NVIDIA runtime fails the whole stack.
    """
    directory = compose_directory(ctx)
    if directory is None:
        return []
    files = [directory / BASE_FILE, directory / APPS_FILE]
    if ctx.answers.get("gpu") and (directory / GPU_FILE).is_file():
        files.append(directory / GPU_FILE)
    return files


def env_file_path(ctx: InitContext) -> Path:
    """The 0600 env file for this instance, inside its own workspace."""
    return ctx.workspace / ENV_FILENAME


def image_tag(ctx: InitContext) -> str:
    """The release tag the stack runs, from the answer or from this build.

    Deliberately NOT read from ``GENUS_IMAGE_TAG`` in the environment: that
    variable configures the compose FILE, not the platform, so nothing in the
    settings model declares it and nothing in Python may quietly depend on it.
    The default is the version of the CLI doing the install, which is the one
    tag guaranteed to match the code that wrote the configuration.
    """
    answered = str(ctx.answers.get("image_tag") or "").strip()
    if answered:
        return answered
    from robothor import __version__

    return f"v{__version__}"


def ready_endpoints(ctx: InitContext) -> dict[str, str]:
    """The four URLs that say the stack is up, on the ports it publishes.

    Read from the settings model, never typed here: the compose file publishes
    the same ports on loopback, and a probe of a port the platform no longer
    serves reports a healthy stack as down.
    """
    settings = ctx.settings
    return {
        "engine": f"http://127.0.0.1:{settings.engine.port}/ready",
        "bridge": f"http://127.0.0.1:{settings.auth.bridge_port}/ready",
        "orchestrator": f"http://127.0.0.1:{settings.services.orchestrator_port}/ready",
        "dashboard": f"http://127.0.0.1:{settings.services.helm_port}/api/ready",
    }


def _report(ctx: InitContext) -> dict[str, Any]:
    """This run's ``compose`` block in the ``--json`` document."""
    block: dict[str, Any] = ctx.report.setdefault("compose", {})
    return block


# ---------------------------------------------------------------------------
# The steps compose owns
# ---------------------------------------------------------------------------


class ComposePrereqsStep(BaseStep):
    """Docker, the Compose plugin, the release files — and a look for a GPU.

    Required, all of it. The old wizard marked every prerequisite optional on
    every substrate, so a box with no Docker passed this check and failed at
    ``up`` — after the identity, the workspace and the fleet had been written.
    """

    id = "prereqs"
    title = "Prerequisites"

    DOCKER_HINT = (
        "install Docker Engine 24+ and the Compose v2 plugin: "
        "https://docs.docker.com/engine/install/"
    )

    @staticmethod
    def _version(result: CommandResult) -> tuple[int, str]:
        """The major version in ``docker --version``, and the text it came from.

        ``(0, text)`` when nothing in the output looks like a version, which is
        what a wrapper script or a permission error produces.
        """
        text = result.text
        for token in text.replace(",", " ").split():
            head = token.split(".", 1)[0].lstrip("v")
            if head.isdigit():
                return int(head), text
        return 0, text

    def check(self, ctx: InitContext) -> CheckResult:
        docker = ctx.run(["docker", "--version"], timeout=PROBE_TIMEOUT_S)
        if not docker.ok:
            return CheckResult(
                False, detail=docker.text or "docker is not installed", fix_hint=self.DOCKER_HINT
            )
        major, text = self._version(docker)
        if major < MINIMUM_DOCKER_MAJOR:
            return CheckResult(
                False,
                detail=f"{text} is older than Docker {MINIMUM_DOCKER_MAJOR}",
                fix_hint=self.DOCKER_HINT,
            )

        compose = ctx.run(["docker", "compose", "version"], timeout=PROBE_TIMEOUT_S)
        if not compose.ok:
            return CheckResult(
                False,
                detail="the docker compose v2 plugin is not installed",
                fix_hint=self.DOCKER_HINT,
            )

        # Optional, and the only thing it decides is one overlay.
        gpu = ctx.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=PROBE_TIMEOUT_S
        )
        ctx.answers["gpu"] = bool(gpu.ok)

        if compose_directory(ctx) is None:
            return CheckResult(
                False,
                detail=f"neither {BASE_FILE} nor {APPS_FILE} is in {ctx.workspace}",
                fix_hint=FETCH_HINT,
            )

        detail = f"{text}; {compose.text}"
        detail += "; a GPU is present" if ctx.answers["gpu"] else "; no GPU (Ollama on the CPU)"
        return CheckResult(True, detail=detail)

    def apply(self, ctx: InitContext) -> None:
        # Everything here is a probe. There is nothing to install: a box that
        # reached phase 2 already has Docker, and this wizard does not install
        # a container runtime behind an operator's back.
        ctx.detail(self.id, "docker and the compose plugin are present")


class ComposeRenderStep(BaseStep):
    """Write the env file, place the identity, and record what will run.

    One file carries every credential, at 0600, inside the instance's own
    workspace. The provider key is resolved at the moment of the write and is
    never stored in ``ctx.answers`` — an answer reaches the plan, the JSON and
    ``init_state.yaml`` the moment anything renders answers.
    """

    id = "render"
    title = "Compose configuration"
    #: Never skipped on a re-run: it is the only thing that rewrites the env
    #: file after a provider, a password or an image tag changes.
    resumable = False

    def check(self, ctx: InitContext) -> CheckResult:
        files = compose_files(ctx)
        if not files:
            return CheckResult(
                False,
                detail=f"no {APPS_FILE} to run",
                fix_hint=FETCH_HINT,
            )
        names = ", ".join(path.name for path in files)
        action = "exists" if env_file_path(ctx).exists() else "create"
        return CheckResult(
            True,
            detail=f"will write {env_file_path(ctx)} (0600) for {names}",
            action=action,
        )

    def apply(self, ctx: InitContext) -> None:
        files = compose_files(ctx)
        if not files:
            raise StepError(f"no {APPS_FILE} to run. {FETCH_HINT}")

        path = env_file_path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, self.body(ctx))
        placed = self._place_identity(ctx)

        tag = image_tag(ctx)
        _report(ctx).update(
            {
                "files": [str(item) for item in files],
                "env_file": str(path),
                "images": {
                    "python": f"{IMAGE_REPOSITORY}/python:{tag}",
                    "app": f"{IMAGE_REPOSITORY}/app:{tag}",
                },
            }
        )
        detail = f"{path} (0600), {len(files)} compose file(s), images tagged {tag}"
        if placed:
            detail += f"; the operator identity is in {placed}"
        ctx.detail(self.id, detail)

    def body(self, ctx: InitContext) -> str:
        """The env file, as text. Pure, so the shape of it is testable."""
        database = ctx.db_config()
        lines = [
            "# Written by `genus init --substrate compose`. Mode 0600 — it holds",
            "# every credential this instance has. Compose reads it twice: as",
            "# --env-file (the ${GENUS_*} variables) and as each service's",
            "# env_file (the ROBOTHOR_* ones).",
            "",
            f"GENUS_IMAGE_TAG={image_tag(ctx)}",
            # The HOST path compose bind-mounts. ROBOTHOR_WORKSPACE is
            # /workspace and is set by the compose file: a host path here would
            # point every container at a directory it does not have.
            f"GENUS_WORKSPACE={ctx.workspace}",
            f"GENUS_ENV_FILE={env_file_path(ctx)}",
            "",
            f"ROBOTHOR_DB_NAME={database['dbname']}",
            f"ROBOTHOR_DB_USER={database['user']}",
            f"ROBOTHOR_DB_PASSWORD={database['password']}",
            f"ROBOTHOR_DB_PORT={database['port']}",
            "",
            # `env` rather than sops or file: a container has no
            # /run/robothor/secrets.env, and nothing fills one for it.
            "ROBOTHOR_SECRETS_BACKEND=env",
        ]

        tenant = str(ctx.answers.get("tenant_id") or "")
        if tenant:
            lines.append(f"ROBOTHOR_DEFAULT_TENANT={tenant}")

        model = str(ctx.answers.get("provider_model") or "")
        if model:
            lines.append(f"ROBOTHOR_LAST_RESORT_MODEL={model}")

        provider_id = str(ctx.answers.get("provider_id") or "")
        credential = self._provider_credential(provider_id)
        if credential:
            name, key = credential
            lines += [
                "",
                "# The provider credential. This file is the only copy on disk.",
                f"{name}={key}",
            ]

        token = str(ctx.answers.get("telegram_token") or "")
        if token:
            lines += ["", f"ROBOTHOR_TELEGRAM_BOT_TOKEN={token}"]

        return "\n".join(lines) + "\n"

    @staticmethod
    def _provider_credential(provider_id: str) -> tuple[str, str] | None:
        """``(variable, key)`` for the chosen provider, resolved here and now.

        Through ``key_pool`` — the instance's one credential resolver — so the
        variable name is the one the platform reads rather than a second,
        drifting table of provider names.
        """
        if not provider_id:
            return None
        from robothor.engine.key_pool import PROVIDERS
        from robothor.init.provider_probe import resolve_provider_key

        spec = next((row for row in PROVIDERS if row.id == provider_id), None)
        if spec is None:
            return None
        key = (resolve_provider_key(provider_id) or "").strip()
        if not key:
            return None
        return spec.env_var, key

    @staticmethod
    def _place_identity(ctx: InitContext) -> Path | None:
        """Copy ``owner.yaml`` into the workspace the containers mount.

        ``~/.robothor`` is not mounted: one workspace mount is the whole
        contract of the release file, and a second mount of the operator's home
        directory is a surprise nobody asked for. The compose file points
        ``ROBOTHOR_OWNER_CONFIG`` at ``/workspace/.robothor/owner.yaml``, so the
        identity has to be inside the workspace — otherwise every container
        loads an instance with no operator.
        """
        from robothor.constants import owner_config_path
        from robothor.settings.sources import owner_config_override_path

        source = owner_config_override_path() or owner_config_path()
        if not source.is_file():
            return None
        target = ctx.workspace / ".robothor" / "owner.yaml"
        if target.resolve() == source.resolve():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_private(target, source.read_text(encoding="utf-8"))
        return target


class ComposeUpStep(BaseStep):
    """One ``docker compose up -d``, with every file it was given."""

    id = "up"
    title = "Start the stack"
    #: Never skipped: a re-run of `genus init` on a stopped box must start it.
    resumable = False

    def command(self, ctx: InitContext) -> list[str]:
        """The exact argv. ``--no-start`` under ``--dry-run``.

        A dry run never reaches ``apply()`` at all (the runner records
        ``planned`` instead), so this is what the plan PRINTS rather than what
        it runs — and if a future caller does execute it, creating the
        containers without starting them is the closest thing to nothing.
        """
        argv = ["docker", "compose", "--env-file", str(env_file_path(ctx))]
        for path in compose_files(ctx):
            argv += ["-f", str(path)]
        argv.append("up")
        return [*argv, "--no-start"] if ctx.dry_run else [*argv, "-d"]

    def check(self, ctx: InitContext) -> CheckResult:
        if not compose_files(ctx):
            return CheckResult(False, detail=f"no {APPS_FILE} to run", fix_hint=FETCH_HINT)
        return CheckResult(True, detail=" ".join(self.command(ctx)))

    def apply(self, ctx: InitContext) -> None:
        argv = self.command(ctx)
        ctx.say("  Starting the stack (this pulls the images on a first run) ...")
        result = ctx.run(argv, timeout=UP_TIMEOUT_S)
        if not result.ok:
            raise StepError(
                f"`docker compose up` exited {result.code}: {result.text}. "
                "Nothing else was configured — fix it and re-run."
            )
        ctx.detail(self.id, f"{len(compose_files(ctx))} compose file(s) up")


class ComposeWaitStep(BaseStep):
    """Poll the four ``/ready`` endpoints until they answer, or say who did not.

    The whole point of the substrate: ``docker compose up -d`` returns as soon
    as the containers are CREATED, so without this the wizard would print a
    first-run link to a dashboard that is still installing its schema.
    """

    id = "wait"
    title = "Wait for the stack"
    resumable = False

    def __init__(self, *, sleep: Callable[[float], None] | None = None) -> None:
        self._sleep = sleep or time.sleep

    @staticmethod
    def _timeout(ctx: InitContext) -> int:
        try:
            return max(1, int(ctx.answers.get("wait_timeout") or DEFAULT_WAIT_TIMEOUT_S))
        except (TypeError, ValueError):
            return DEFAULT_WAIT_TIMEOUT_S

    def check(self, ctx: InitContext) -> CheckResult:
        names = ", ".join(ready_endpoints(ctx))
        return CheckResult(True, detail=f"will wait up to {self._timeout(ctx)}s for {names}")

    def apply(self, ctx: InitContext) -> None:
        endpoints = ready_endpoints(ctx)
        ready = dict.fromkeys(endpoints, False)
        remaining = float(self._timeout(ctx))

        while True:
            for name, url in endpoints.items():
                if ready[name]:
                    continue
                # A 401 or 403 is UP: the bridge gates its health route behind
                # auth in production, and a service enforcing authentication is
                # a service that started.
                response = ctx.http("GET", url)
                if response.status and response.status < 500:
                    ready[name] = True
                    ctx.say(f"    {name}: ready")
            if all(ready.values()) or remaining <= 0:
                break
            self._sleep(READY_POLL_INTERVAL_S)
            remaining -= READY_POLL_INTERVAL_S

        _report(ctx)["ready"] = dict(ready)
        down = [name for name, up in ready.items() if not up]
        if down:
            raise StepError(
                f"{', '.join(down)} did not answer /ready within {self._timeout(ctx)}s. "
                "Check `docker compose logs " + " ".join(down) + "`; the containers are "
                "running and `genus init` resumes where it stopped."
            )
        ctx.detail(self.id, f"{len(ready)} services ready")


class ComposeModelsStep(ModelsStep):
    """Pull the RAG models — after ``up``, not before it.

    ``ModelsStep`` probes Ollama in ``check()`` and skips when nothing answers.
    On this substrate nothing CAN answer during phase 1: the Ollama container
    is started by the ``up`` step, which has not run yet. Inherited unchanged,
    that check turned the pull into a step that could never execute on the
    substrate that needed it most — a green run, and no embeddings for memory
    search, forever.
    """

    def check(self, ctx: InitContext) -> CheckResult:
        if ctx.answers.get("skip_models"):
            return CheckResult(True, detail="skipped (--skip-models)", action="skip")
        from robothor.setup import REQUIRED_MODELS

        return CheckResult(True, detail="will pull " + ", ".join(REQUIRED_MODELS))


class ComposeSubstrate:
    """Genus OS as four containers beside PostgreSQL, Redis and Ollama."""

    name = "compose"

    def steps(self) -> Sequence[Step]:
        return (
            AckStep(),
            SubstrateStep(),
            ComposePrereqsStep(),
            DetectStep(),
            ProviderStep(),
            IdentityStep(),
            # The workspace and the fleet are seeded BEFORE the stack starts:
            # the engine's /ready refuses an empty fleet
            # (ROBOTHOR_REQUIRED_AGENT_IDS=main), and the workspace is a bind
            # mount, so a manifest written afterwards would arrive after the
            # readiness gate the `wait` step is holding.
            WorkspaceStep(),
            AgentsStep(),
            ComposeRenderStep(),
            ComposeUpStep(),
            ComposeWaitStep(),
            ComposeModelsStep(),
            # Everything below talks to the database the stack just started and
            # the `migrate` service just built. `starts_later` is what stops
            # phase 1 connecting to a container that does not exist yet.
            DatabaseStep(starts_later="the compose stack"),
            OperatorStep(),
            ChannelsStep(),
            SecretsStep(),
            VerifyStep(),
            LocalLinkStep(substrate=self),
        )

    def first_run_url(self, ctx: InitContext) -> str:
        """Loopback, on the port the dashboard container publishes.

        The same URL shape as the local substrate on purpose: the dashboard is
        published on ``127.0.0.1:3004`` and reached from elsewhere through a
        tunnel or an ``ssh -L``, which the link step prints when nobody is at
        this keyboard. Guessing a public hostname would hand the operator a URL
        that does not resolve and a token that has already started expiring.
        """
        from robothor import setup_token
        from robothor.setup import helm_port

        token = setup_token.create_setup_token(ctx.workspace)
        return setup_token.setup_link("127.0.0.1", helm_port(), token)


def substrate() -> ComposeSubstrate:
    """Factory, so every substrate module has the same shape."""
    return ComposeSubstrate()


def _write_private(path: Path, body: str) -> None:
    """Write a file only its owner can read, without a readable moment.

    ``open()`` then ``chmod()`` leaves the content world-readable for as long
    as it takes to run the second call; ``os.open`` with the mode does not.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
    # An existing file keeps its old mode through O_CREAT, so say it again.
    path.chmod(0o600)
