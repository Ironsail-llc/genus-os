"""The unit the wizard is made of: one step, checked before it is applied.

A step answers two questions, and the split between them is the whole design.
``check()`` says what WOULD happen and must not write anything; ``apply()``
does it. Phase 1 calls every ``check()`` and prints the result, so an operator
sees the complete plan -- and ``--yes`` refuses the whole run -- before the
first byte lands on disk.

A step that fails says so by raising :class:`StepError` with a sentence an
operator can act on. It never calls ``sys.exit`` and never prints its own
verdict: the runner owns the exit code and the renderer owns the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.init.context import InitContext

__all__ = [
    "ACTIONS",
    "AckStep",
    "AgentsStep",
    "BaseStep",
    "ChannelsStep",
    "CheckResult",
    "DatabaseStep",
    "DetectStep",
    "IdentityStep",
    "MigrateStep",
    "ModelsStep",
    "OperatorStep",
    "PrereqsStep",
    "ProviderStep",
    "SECRETS_BACKENDS",
    "SecretsStep",
    "Step",
    "StepError",
    "SubstrateStep",
    "VerifyStep",
    "WorkspaceStep",
]

#: What ``check()`` says phase 2 will do with this step.
#:
#: ``create`` -- it will run. ``exists`` -- the thing is already there, and
#: ``apply()`` is an idempotent refresh rather than a first write. ``skip`` --
#: it will not run at all (a flag turned it off, or it does not apply here).
ACTIONS = ("create", "exists", "skip")


class StepError(RuntimeError):
    """A step could not do its job. The message is shown to the operator."""


@dataclass(frozen=True)
class CheckResult:
    """What one step found, without changing anything.

    Args:
        ok: False means this step cannot succeed as things stand. For a
            required step that stops the run before anything is written.
        detail: one line, for the plan and for ``--json``.
        fix_hint: what the operator should do about a failure. Empty when
            ``ok`` -- a hint attached to a passing check is noise.
        action: one of :data:`ACTIONS`.
    """

    ok: bool
    detail: str = ""
    fix_hint: str = ""
    action: str = "create"

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, not {self.action!r}")


@runtime_checkable
class Step(Protocol):
    """One thing ``genus init`` does.

    Attributes:
        id: stable identifier. It is the key in ``init_state.yaml`` and in the
            ``--json`` payload, so it is part of the interface and does not
            change with a rename of the class.
        title: what an operator calls this step.
        required: a failing ``check()`` on a required step blocks the run.
        resumable: whether a completed run of this step may be skipped on a
            re-run. ``False`` for steps that must happen every time, such as
            the security acknowledgement.
    """

    id: str
    title: str
    required: bool
    resumable: bool

    def check(self, ctx: InitContext) -> CheckResult: ...

    def apply(self, ctx: InitContext) -> None: ...


class BaseStep:
    """Defaults for a step: required, resumable, and a check that passes.

    Subclasses override what they need. Having a base at all means a step is
    three lines when it has nothing to check, which is how the wizard stays
    readable at seventeen of them.
    """

    id: str = ""
    title: str = ""
    required: bool = True
    resumable: bool = True

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True)

    def apply(self, ctx: InitContext) -> None:
        return None


# ---------------------------------------------------------------------------
# The core steps, in the order the wizard runs them. A substrate assembles
# them (see robothor/init/substrates/) and adds the two that are its own:
# how services start, and what the first-run URL looks like.
# ---------------------------------------------------------------------------


class AckStep(BaseStep):
    """One line about what this command is about to do to this machine."""

    id = "ack"
    title = "Security note"
    #: Never skipped on a re-run. It is one line and it is the last moment
    #: before the wizard writes an identity and mints a sign-in link.
    resumable = False

    NOTE = (
        "genus init writes an operator identity to ~/.robothor/owner.yaml, a "
        "configuration to this workspace, and a single-use sign-in link for "
        "this machine. Run it as the account that will own the instance."
    )

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True, detail=self.NOTE)

    def apply(self, ctx: InitContext) -> None:
        if ctx.yes:
            ctx.detail(self.id, "accepted (--yes)")
            return
        ctx.say(f"  {self.NOTE}")
        if not ctx.confirm("ack", "Continue?", default=True):
            raise StepError("cancelled at the security note; nothing was written")
        ctx.detail(self.id, "accepted")


class SubstrateStep(BaseStep):
    """Which substrate's steps are in this plan."""

    id = "substrate"
    title = "Substrate"

    def check(self, ctx: InitContext) -> CheckResult:
        from robothor.init.substrate import AVAILABLE_SUBSTRATES

        name = ctx.substrate_name
        if name not in AVAILABLE_SUBSTRATES:
            return CheckResult(
                False,
                detail=f"{name} is not available in this release",
                fix_hint=(
                    "use --substrate "
                    + "|".join(AVAILABLE_SUBSTRATES)
                    + "; compose, systemd and helm land in later releases"
                ),
            )
        return CheckResult(True, detail=name)

    def apply(self, ctx: InitContext) -> None:
        ctx.answers["substrate"] = ctx.substrate_name
        ctx.detail(self.id, ctx.substrate_name)


class PrereqsStep(BaseStep):
    """The tools this substrate cannot run without.

    Which ones those are is the substrate's call, not a global constant. The
    old wizard marked PostgreSQL and Redis optional on every install, so a box
    with neither reported a green prerequisite check and then failed at the
    migration -- three steps and one identity file later.
    """

    id = "prereqs"
    title = "Prerequisites"

    def __init__(
        self,
        *,
        required: Sequence[str] = (),
        prereqs: Callable[..., list[dict[str, Any]]] | None = None,
    ) -> None:
        self.required_names = tuple(required)
        self._prereqs = prereqs

    def _run(self, ctx: InitContext) -> list[dict[str, Any]]:
        check_prerequisites = self._prereqs
        if check_prerequisites is None:
            from robothor.setup import check_prerequisites as real

            check_prerequisites = real
        return check_prerequisites(
            required=self.required_names,
            docker_required=bool(ctx.answers.get("docker")),
        )

    def check(self, ctx: InitContext) -> CheckResult:
        found = self._run(ctx)
        missing = [row for row in found if row["required"] and not row["found"]]
        if missing:
            return CheckResult(
                False,
                detail="missing: " + ", ".join(row["name"] for row in missing),
                fix_hint="; ".join(row["hint"] for row in missing if row["hint"]),
            )
        present = [row["name"] for row in found if row["found"]]
        absent = [row["name"] for row in found if not row["found"]]
        detail = f"{len(present)} present"
        if absent:
            detail += f"; optional and absent: {', '.join(absent)}"
        return CheckResult(True, detail=detail)

    def apply(self, ctx: InitContext) -> None:
        if not ctx.answers.get("docker"):
            ctx.detail(self.id, "nothing to install")
            return
        from robothor.setup import generate_docker_compose, wait_for_services

        password = str(ctx.answers.get("db_password") or "")
        compose_path = generate_docker_compose(ctx.workspace, password)
        started = wait_for_services(ctx.workspace, timeout=90)
        ctx.detail(
            self.id,
            f"{compose_path} " + ("is up" if started else "written; containers still starting"),
        )


class DetectStep(BaseStep):
    """What this box already has, so the wizard asks for less.

    Never required: detecting nothing is a normal fresh install, and blocking
    on it would make an offline box unable to start.
    """

    id = "detect"
    title = "Detect what is already here"
    required = False

    def _detect(self, ctx: InitContext) -> str:
        from robothor.init.provider_probe import (
            detect_codex_login,
            detect_ollama_tool_models,
            detect_provider_keys,
        )

        providers = detect_provider_keys()
        ctx.answers["detected_providers"] = providers
        parts = [f"{row.label} ({len(row.slots)} key slot(s))" for row in providers]

        local = detect_ollama_tool_models(ctx)
        ctx.answers["detected_ollama"] = local
        if local:
            parts.append(f"Ollama: {len(local)} tool-capable model(s)")

        codex = detect_codex_login(ctx.settings.paths.codex_home)
        ctx.answers["detected_codex"] = codex
        if codex:
            parts.append("a Codex login")

        return ", ".join(parts) if parts else "no provider credential or local model found"

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True, detail=self._detect(ctx))

    def apply(self, ctx: InitContext) -> None:
        ctx.detail(self.id, self._detect(ctx))


class ProviderStep(BaseStep):
    """Choose a model, and PROVE the instance can reach it.

    A failed probe blocks. An install that writes a provider it has never
    successfully called is the failure this whole wizard was rebuilt to
    prevent: everything reports green until the operator's first message.
    """

    id = "provider"
    title = "Model provider"

    def __init__(self, *, probe: Callable[..., Any] | None = None) -> None:
        self._probe = probe

    def _candidates(self, ctx: InitContext) -> tuple[str, str]:
        """The provider id and model this step will use, before any question."""
        detected = list(ctx.answers.get("detected_providers") or [])
        local = list(ctx.answers.get("detected_ollama") or [])
        provider_id = str(ctx.answers.get("provider_id") or "")
        model = str(ctx.answers.get("provider_model") or "")
        if not provider_id:
            if detected:
                provider_id = detected[0].id
            elif local:
                provider_id = "ollama"
        if not model:
            if provider_id == "ollama" and local:
                model = f"ollama/{local[0]}"
            else:
                for row in detected:
                    if row.id == provider_id:
                        model = row.default_model
                        break
        return provider_id, model

    def check(self, ctx: InitContext) -> CheckResult:
        _provider_id, model = self._candidates(ctx)
        if not model and not ctx.answers.get("provider_key"):
            return CheckResult(
                False,
                detail="no provider credential and no tool-capable local model found",
                fix_hint=(
                    "export a provider key (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, "
                    "OPENAI_API_KEY, GEMINI_API_KEY or DEEPSEEK_API_KEY) and re-run, "
                    "or pull a tool-capable Ollama model"
                ),
            )
        if ctx.offline:
            return CheckResult(True, detail=f"{model} (recorded unprobed, --offline)")
        return CheckResult(True, detail=f"will test {model} with a 1-token completion")

    def apply(self, ctx: InitContext) -> None:
        from robothor.init.provider_probe import (
            models_for_provider,
            probe_model,
            record_unprobed,
        )

        provider_id, model = self._candidates(ctx)
        provider_id = ctx.ask("provider_id", "Provider", provider_id)
        choices = models_for_provider(provider_id)
        if choices and not ctx.yes:
            ctx.say("    Models: " + ", ".join(choices[:8]))
        model = ctx.ask("provider_model", f"Model for {provider_id}", model or "")
        if not model:
            raise StepError("no model was chosen, so there is nothing to test")

        if ctx.offline:
            result = record_unprobed(provider_id, model)
        else:
            probe = self._probe or probe_model
            result = probe(model, api_key=ctx.answers.get("provider_key") or None)
        if not result.ok:
            raise StepError(
                f"the provider probe failed for {model}: {result.detail}. "
                "Nothing further was configured — fix the credential and re-run."
            )

        ctx.answers["provider_id"] = provider_id
        ctx.answers["provider_model"] = model
        ctx.answers["provider_probed"] = result.probed
        ctx.write_setting("ROBOTHOR_LAST_RESORT_MODEL", model)
        ctx.detail(self.id, f"{model}: {result.detail}")


class IdentityStep(BaseStep):
    """The operator's name, email and tenant — written to owner.yaml only."""

    id = "identity"
    title = "Operator identity"

    def _path(self, ctx: InitContext) -> Any:
        from robothor.constants import owner_config_path

        return owner_config_path()

    def check(self, ctx: InitContext) -> CheckResult:
        path = self._path(ctx)
        if path.exists():
            return CheckResult(True, detail=f"{path} already names the operator", action="exists")
        name = str(ctx.answers.get("owner_name") or "")
        email = str(ctx.answers.get("owner_email") or "")
        if ctx.yes and not (name and email):
            return CheckResult(
                False,
                detail="no operator name and email to write",
                fix_hint=(
                    "set ROBOTHOR_OWNER_NAME and ROBOTHOR_OWNER_EMAIL, pass --owner-name/"
                    "--owner-email, or run without --yes"
                ),
            )
        return CheckResult(True, detail=f"will write {path}")

    def apply(self, ctx: InitContext) -> None:
        from robothor.constants import DEFAULT_TENANT
        from robothor.init.identity import Identity, write_identity

        identity = Identity(
            name=ctx.ask("owner_name", "Your name"),
            email=ctx.ask("owner_email", "Your email"),
            tenant_id=ctx.ask("tenant_id", "Tenant id", DEFAULT_TENANT) or DEFAULT_TENANT,
        )
        if not identity.complete:
            raise StepError("an operator needs both a name and an email address")
        ctx.answers["identity"] = identity
        path = self._path(ctx)
        if write_identity(identity, path=path):
            ctx.detail(self.id, f"wrote {path}")
        else:
            ctx.detail(self.id, f"{path} already exists; left alone")


class WorkspaceStep(BaseStep):
    """The directory tree, its brain/ scaffold, and the template variables."""

    id = "workspace"
    title = "Workspace"

    def check(self, ctx: InitContext) -> CheckResult:
        if (ctx.workspace / "brain").is_dir():
            return CheckResult(True, detail=f"{ctx.workspace} exists", action="exists")
        return CheckResult(True, detail=f"will create {ctx.workspace}")

    def apply(self, ctx: InitContext) -> None:
        from robothor.setup import create_workspace, resolve_workspace_templates

        create_workspace(ctx.workspace)
        identity = ctx.answers.get("identity")
        resolve_workspace_templates(
            ctx.workspace,
            ai_name=str(ctx.answers.get("ai_name") or ctx.settings.channels.ai_name),
            ai_email=str(ctx.answers.get("ai_email") or ctx.settings.channels.ai_email),
            owner_name=getattr(identity, "name", ""),
            owner_email=getattr(identity, "email", ""),
        )
        ctx.detail(self.id, str(ctx.workspace))


class DatabaseStep(BaseStep):
    """Where PostgreSQL is, proved by connecting to it before writing it down."""

    id = "database"
    title = "Database"

    FIX = (
        "start PostgreSQL and set ROBOTHOR_DB_HOST / ROBOTHOR_DB_PORT / ROBOTHOR_DB_NAME / "
        "ROBOTHOR_DB_USER / ROBOTHOR_DB_PASSWORD, or pass --skip-db to configure it later"
    )

    def check(self, ctx: InitContext) -> CheckResult:
        if ctx.answers.get("skip_db"):
            return CheckResult(True, detail="skipped (--skip-db)", action="skip")
        try:
            connection = ctx.db()
        except Exception as exc:  # noqa: BLE001 - an unreachable DB is the finding
            return CheckResult(False, detail=f"{exc}".strip(), fix_hint=self.FIX)
        close = getattr(connection, "close", None)
        if callable(close):
            close()
        config = ctx.db_config()
        return CheckResult(
            True, detail=f"{config['user']}@{config['host']}:{config['port']}/{config['dbname']}"
        )

    def apply(self, ctx: InitContext) -> None:
        config = ctx.db_config()
        ctx.write_setting("ROBOTHOR_DB_HOST", config["host"])
        ctx.write_setting("ROBOTHOR_DB_PORT", config["port"])
        ctx.write_setting("ROBOTHOR_DB_NAME", config["dbname"])
        ctx.write_setting("ROBOTHOR_DB_USER", config["user"])
        ctx.detail(self.id, f"{config['host']}:{config['port']}/{config['dbname']}")


class MigrateStep(BaseStep):
    """The canonical migrator, and nothing else.

    Not a glob of ``migrations/*.sql``, not ``psql -f``: the manifest-driven
    ``robothor.db.migrate.apply`` is the only path that records a ledger row,
    and a schema created outside it is a schema nothing can upgrade.
    """

    id = "migrate"
    title = "Database migrations"

    def __init__(self, *, migrator: Callable[..., Any] | None = None) -> None:
        self._migrator = migrator

    def check(self, ctx: InitContext) -> CheckResult:
        if ctx.answers.get("skip_db"):
            return CheckResult(True, detail="skipped (--skip-db)", action="skip")
        return CheckResult(True, detail="will apply the migration manifest")

    def apply(self, ctx: InitContext) -> None:
        from robothor.db.migrate import MigrationError

        migrate = self._migrator
        if migrate is None:
            from robothor.db.migrate import apply as real

            migrate = real

        connection = ctx.db()
        try:
            migrate(connection=connection)
        except MigrationError as exc:
            # The migrator names its own remedy (--adopt-baseline,
            # --adopt-through). Flattening it into "migration failed" sends the
            # operator to connection troubleshooting for a schema problem.
            raise StepError(f"the migrator refused to run: {exc}") from exc
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        ctx.detail(self.id, "schema is at the head of the manifest")


class ModelsStep(BaseStep):
    """Ollama pulls, and only when Ollama is the model this instance uses."""

    id = "models"
    title = "Local models"
    required = False

    def __init__(self, *, puller: Callable[[str, list[str]], None] | None = None) -> None:
        self._puller = puller

    def check(self, ctx: InitContext) -> CheckResult:
        if ctx.answers.get("skip_models"):
            return CheckResult(True, detail="skipped (--skip-models)", action="skip")
        if str(ctx.answers.get("provider_id") or "") != "ollama":
            return CheckResult(
                True, detail="skipped (the chosen provider is not Ollama)", action="skip"
            )
        from robothor.setup import REQUIRED_MODELS

        return CheckResult(True, detail="will pull " + ", ".join(REQUIRED_MODELS))

    def apply(self, ctx: InitContext) -> None:
        from robothor.setup import REQUIRED_MODELS

        pull = self._puller
        if pull is None:
            from robothor.setup import pull_ollama_models

            pull = pull_ollama_models
        pull(str(ctx.settings.ollama.url).rstrip("/"), list(REQUIRED_MODELS))
        ctx.detail(self.id, "pulled " + ", ".join(REQUIRED_MODELS))


class AgentsStep(BaseStep):
    """Install a fleet preset, in-process.

    Through ``cli.agent.install_preset`` rather than a subprocess: a shelled-out
    CLI runs a different interpreter against a different workspace and reports
    a partial install as success, and its only channel back is parsed stdout.
    """

    id = "agents"
    title = "Agents"
    required = False

    def __init__(self, *, installer: Callable[..., dict[str, Any]] | None = None) -> None:
        self._installer = installer

    def _preset(self, ctx: InitContext) -> str:
        return str(ctx.answers.get("preset") or ctx.settings.substrate.init_preset)

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True, detail=f"preset {self._preset(ctx)}")

    def apply(self, ctx: InitContext) -> None:
        install = self._installer
        if install is None:
            from robothor.cli.agent import install_preset

            install = install_preset

        preset = self._preset(ctx)
        outcome = install(preset, auto_yes=True)
        if outcome.get("unknown_preset"):
            available = ", ".join(outcome.get("available") or [])
            raise StepError(f"no preset named {preset!r}; the presets are {available}")

        installed = outcome.get("installed") or []
        detail = f"{len(installed)} of {outcome.get('requested', 0)} agents installed"
        failed = outcome.get("failed") or {}
        missing = outcome.get("missing") or []
        if failed:
            detail += "; failed: " + ", ".join(sorted(failed))
        if missing:
            detail += "; no template for: " + ", ".join(sorted(missing))
        ctx.detail(self.id, detail)


class OperatorStep(BaseStep):
    """Seed the operator's account in the tenant owner.yaml names."""

    id = "operator"
    title = "Operator account"

    def __init__(self, *, bootstrap: Callable[[], dict[str, Any] | None] | None = None) -> None:
        self._bootstrap = bootstrap

    def check(self, ctx: InitContext) -> CheckResult:
        if ctx.answers.get("skip_db"):
            return CheckResult(
                True,
                detail="skipped (--skip-db: the account lives in the database)",
                action="skip",
            )
        return CheckResult(True, detail="will seed the owner account (no password)")

    def apply(self, ctx: InitContext) -> None:
        from robothor.constants import DEFAULT_TENANT
        from robothor.init.identity import Identity, bootstrap_operator

        identity = ctx.answers.get("identity")
        if not isinstance(identity, Identity):
            from robothor.owner_config import load_owner_config

            owner = load_owner_config()
            if owner is None:
                raise StepError("no owner.yaml to seed an account from; re-run the identity step")
            identity = Identity(
                name=" ".join(p for p in (owner.first_name, owner.last_name) if p),
                email=owner.email,
                tenant_id=owner.tenant_id or DEFAULT_TENANT,
            )
        account = bootstrap_operator(identity, bootstrap=self._bootstrap)
        ctx.detail(self.id, f"owner account in tenant {account.get('tenant_id')} (no password yet)")


class ChannelsStep(BaseStep):
    """Optional. A Telegram token, verified with ``getMe`` before it is kept.

    ``genus init`` never asks for this unless the operator opted in: the daemon
    has been Telegram-optional for releases, and a wizard that demands a bot
    token is a wizard nobody can finish without one.
    """

    id = "channels"
    title = "Channels"
    required = False

    @staticmethod
    def _username(body: str) -> str:
        import json

        try:
            name = json.loads(body).get("result", {}).get("username", "")
        except Exception:  # noqa: BLE001 - a token that works but answers oddly
            return ""
        return str(name or "")

    def check(self, ctx: InitContext) -> CheckResult:
        token = str(ctx.answers.get("telegram_token") or "")
        if not token:
            return CheckResult(True, detail="no channel configured", action="skip")
        if ctx.offline:
            return CheckResult(True, detail="token not verified (--offline)", action="skip")
        response = ctx.http("GET", f"https://api.telegram.org/bot{token}/getMe")
        if not response.ok:
            # The token is in the URL, so nothing about this request may be
            # echoed back: the status code is the whole safe answer.
            return CheckResult(
                False,
                detail=f"Telegram rejected the bot token (HTTP {response.status})",
                fix_hint="check the token with @BotFather, or omit --telegram-token",
            )
        username = self._username(response.body)
        return CheckResult(True, detail=f"Telegram bot @{username}" if username else "Telegram bot")

    def apply(self, ctx: InitContext) -> None:
        token = str(ctx.answers.get("telegram_token") or "")
        response = ctx.http("GET", f"https://api.telegram.org/bot{token}/getMe")
        username = self._username(response.body)
        if username:
            ctx.write_setting("ROBOTHOR_TELEGRAM_BOT_NAME", username)
        ctx.detail(self.id, f"Telegram bot @{username}" if username else "Telegram configured")


#: Where an instance's credentials come from. ``env`` means they are already
#: in the unit environment, which is the only one of the three that needs no
#: further setup — so it is the local default rather than SOPS.
SECRETS_BACKENDS = ("env", "file", "sops")


class SecretsStep(BaseStep):
    """Pick a secrets backend, and give the instance somewhere to keep keys."""

    id = "secrets"
    title = "Secrets"
    required = False

    def _backend(self, ctx: InitContext) -> str:
        return str(ctx.answers.get("secrets_backend") or "env")

    def check(self, ctx: InitContext) -> CheckResult:
        backend = self._backend(ctx)
        if backend not in SECRETS_BACKENDS:
            return CheckResult(
                False,
                detail=f"{backend} is not a secrets backend",
                fix_hint="--secrets-backend must be one of " + ", ".join(SECRETS_BACKENDS),
            )
        return CheckResult(True, detail=f"backend {backend}")

    def apply(self, ctx: InitContext) -> None:
        backend = self._backend(ctx)
        ctx.write_setting("ROBOTHOR_SECRETS_BACKEND", backend)

        from robothor.setup import setup_vault_key

        setup_vault_key(ctx.workspace)
        detail = f"backend {backend}; vault master key present"

        key = str(ctx.answers.get("provider_key") or "")
        provider_id = str(ctx.answers.get("provider_id") or "")
        if key and provider_id and not ctx.answers.get("skip_db"):
            detail += "; " + self._store_provider_key(ctx, provider_id, key)
        elif key:
            detail += "; the provider key was NOT stored (no database) — export it in the unit"
        ctx.detail(self.id, detail)

    def _store_provider_key(self, ctx: InitContext, provider_id: str, key: str) -> str:
        """Put the probed credential where ``key_pool`` will find it.

        Slot 1 of the instance vault, the same place the Settings page writes.
        A failure here is reported, not raised: the install is otherwise
        complete and the operator can export the variable instead.
        """
        try:
            from robothor.vault.crypto import get_master_key
            from robothor.vault.dal import set_secret
            from robothor.vault.naming import provider_key

            identity = ctx.answers.get("identity")
            tenant = getattr(identity, "tenant_id", None)
            set_secret(
                provider_key(provider_id, 1),
                key,
                get_master_key(ctx.workspace),
                **({"tenant_id": tenant} if tenant else {}),
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            return f"the provider key could not be stored ({type(exc).__name__}); export it instead"
        return f"the provider key is in the vault ({provider_id} slot 1)"


class VerifyStep(BaseStep):
    """``genus doctor``, required checks only. Zero failures or exit 1.

    The same runner the CLI, the bridge and the install gate use, so "init
    said it was healthy" and "doctor says it is healthy" cannot disagree.
    """

    id = "verify"
    title = "Verify"
    resumable = False

    def __init__(self, *, doctor: Callable[[Any], Any] | None = None) -> None:
        self._doctor = doctor

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True, detail="will run genus doctor's required checks")

    def apply(self, ctx: InitContext) -> None:
        from robothor.doctor.context import DoctorContext

        run = self._doctor
        if run is None:
            from robothor.doctor.runner import run_sync

            run = run_sync

        # Offline unless the provider probe actually ran: a verification step
        # must not be the thing that first spends money on an instance, and
        # `provider.completion` is the only check that would.
        offline = not bool(ctx.answers.get("provider_probed"))
        report = run(DoctorContext(offline=offline))
        failed = [
            row for row in report.results if row.status == "fail" and row.severity == "required"
        ]
        if failed:
            named = "; ".join(f"{row.id} ({row.detail})" for row in failed)
            raise StepError(f"genus doctor found {len(failed)} required failure(s): {named}")
        ctx.detail(self.id, f"genus doctor: 0 required failures, {len(report.results)} checks")
