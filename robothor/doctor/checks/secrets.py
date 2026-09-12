"""Where this instance keeps its credentials, and whether that place works.

The 2026-09-03 sign-in outage is the whole argument for this category. The
bridge booted before its secrets were decrypted, the secret check failed CLOSED
and SILENTLY, and ``app.robothor.ai`` answered 403 to every login for eight
days while the unit reported ``active`` and ``/ready`` reported ready. The
question "is this credential resolvable, and from where?" had no command.

None of these checks returns a value. They return a SOURCE -- ``env``,
``vault``, ``missing``, ``unavailable`` -- which is the whole answer an
operator needs and discloses nothing.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS", "resolve_backend"]

#: The two modes ``scripts/load-secrets.sh`` accepts for a plaintext secrets
#: file. Anything else means a third party can rewrite what every service loads
#: into its environment -- credential substitution, not a hygiene nit.
_ALLOWED_MODES = (0o600, 0o400)

#: The vault row the signing key has always lived in. It exports to the
#: environment as ``AUTH_JWT_SIGNING_KEY``, so a lookup under the settings name
#: alone would miss it and conclude the instance has none -- and the caller
#: that acts on "none" GENERATES one, invalidating every session and making
#: every stored MFA secret undecryptable.
_SIGNING_KEY_VAULT_ROW = "auth/jwt_signing_key"


def resolve_backend(configured: str, root: str, backend_file: str) -> tuple[str, bool, list[str]]:
    """Which backend ``load-secrets.sh`` would choose, and what is wrong with it.

    A second implementation of the shell script's dispatch, deliberately: the
    script runs as root from ``ExecStartPre`` and cannot be invoked by a
    diagnostic, but its rules are four lines and the question "which backend
    will this box use on its next boot?" is one an operator has to be able to
    ask BEFORE the boot. The rules are: an explicit ``ROBOTHOR_SECRETS_BACKEND``
    wins; otherwise an encrypted store means ``sops``, else a plaintext file
    means ``file``, else ``env``.

    Returns ``(backend, auto_detected, problems)``.
    """
    prefix = root.rstrip("/")
    sops_file = Path(f"{prefix}/etc/robothor/secrets.enc.json")
    plain_file = Path(backend_file or f"{prefix}/etc/robothor/secrets.env")

    backend = (configured or "").strip().lower()
    auto = not backend
    if auto:
        if sops_file.is_file():
            backend = "sops"
        elif plain_file.is_file():
            backend = "file"
        else:
            backend = "env"

    problems: list[str] = []
    if backend not in {"sops", "file", "env"}:
        problems.append(
            f"ROBOTHOR_SECRETS_BACKEND={backend!r} is not one of sops, file, env — "
            "load-secrets.sh will refuse to start the platform"
        )
        return backend, auto, problems

    if backend == "sops" and not sops_file.is_file():
        problems.append(f"{sops_file} does not exist, so the sops backend has nothing to decrypt")
    if backend == "file":
        problems.extend(_file_problems(plain_file))
    return backend, auto, problems


def _file_problems(path: Path) -> list[str]:
    """Everything ``load-secrets.sh`` would refuse the plaintext file for."""
    problems: list[str] = []
    try:
        info = path.lstat()
    except OSError:
        return [f"{path} does not exist (set ROBOTHOR_SECRETS_BACKEND_FILE, or use env)"]
    if stat.S_ISLNK(info.st_mode):
        return [f"{path} is a symlink; load-secrets.sh requires a regular file"]
    if not stat.S_ISREG(info.st_mode):
        return [f"{path} is not a regular file"]
    mode = stat.S_IMODE(info.st_mode)
    if mode not in _ALLOWED_MODES:
        problems.append(
            f"{path} is mode {mode:04o}: it must be 0600 or 0400, so that only its owner "
            "can read this instance's credentials"
        )
    # root, or the account asking -- the same rule load-secrets.sh applies,
    # where "the account asking" is the service account running ExecStartPre.
    if info.st_uid not in (0, os.getuid()):
        problems.append(
            f"{path} is owned by uid {info.st_uid}: it must be owned by root or by the "
            "account the services run as, or a third party can substitute credentials"
        )
    return problems


async def _backend(ctx: DoctorContext) -> Result:
    """The secrets backend this box would use on its next boot is usable.

    A failure here is a boot that starts four services with no credentials
    behind a unit that reports ``active (exited)`` -- the shape of the eight-day
    sign-in outage. The detail names the backend and the path, never anything
    read from it.
    """
    settings = ctx.settings.secrets
    backend, auto, problems = await ctx.run_blocking(
        resolve_backend, settings.backend, settings.backend_root, settings.backend_file
    )
    how = "auto-detected" if auto else "ROBOTHOR_SECRETS_BACKEND"
    if problems:
        return fail(f"backend {backend} ({how}): " + "; ".join(problems))
    if backend == "env" and auto:
        return ok(
            "backend env (auto-detected): no secrets file found, so credentials are "
            "expected in the unit or container environment"
        )
    return ok(f"backend {backend} ({how})")


async def _signing_key(ctx: DoctorContext) -> Result:
    """The JWT signing key resolves, or is known to be absent.

    The three answers are genuinely different. ``env``/``vault``: sessions
    survive a restart. ``missing``: there is none yet and one will be generated
    on first boot -- fine for a fresh install, and information, not a failure.
    ``unavailable``: the vault could not be READ, and that is the dangerous
    one. The generate-on-absence path upserts, so a vault whose read fails
    while its write works would overwrite the live signing key and invalidate
    every session and every stored MFA secret. Probed live rather than through
    the five-minute failure cooldown, because a stale verdict is exactly what
    must not be acted on here.
    """

    def _source() -> str:
        from robothor.secrets import secret_source

        return secret_source("GENUS_AUTH_SIGNING_KEY", vault_key=_SIGNING_KEY_VAULT_ROW, live=True)

    source = await ctx.run_blocking(_source)
    if source == "unavailable":
        return fail(
            "vault unreadable — the signing key cannot be resolved, and generating a "
            "replacement would invalidate every session and every stored MFA secret"
        )
    if source == "missing":
        return ok("not set; one will be generated and stored on first boot")
    return ok(f"resolved from the {source}")


async def _bridge_sso(ctx: DoctorContext) -> Result:
    """``GENUS_BRIDGE_SSO_SECRET`` is set wherever authentication is enforced.

    Without it the bridge refuses every ``/api/auth/sso`` exchange, so nobody
    can sign in -- while the process is up and ``/ready`` was, for eight days,
    green. Skipped on a loopback development bridge that performs no exchange:
    demanding it there would fail a check nothing can pass.
    """

    def _probe() -> tuple[bool, str]:
        from robothor.auth.runtime import auth_required
        from robothor.secrets import secret_source

        return auth_required(), secret_source("GENUS_BRIDGE_SSO_SECRET")

    required, source = await ctx.run_blocking(_probe)
    if not required:
        return skip("this bridge does not enforce authentication (loopback development mode)")
    if source in {"env", "vault"}:
        return ok(f"resolved from the {source}")
    if source == "unavailable":
        return fail("vault unreadable, so GENUS_BRIDGE_SSO_SECRET cannot be resolved")
    return fail(
        "GENUS_BRIDGE_SSO_SECRET is not set, so the bridge will refuse every SSO "
        "exchange and nobody will be able to sign in"
    )


CHECKS: tuple[Check, ...] = (
    Check(
        id="secrets.backend",
        title="The secrets backend is usable",
        category="secrets",
        severity="required",
        run=_backend,
    ),
    Check(
        id="secrets.signing_key",
        title="The JWT signing key resolves",
        category="secrets",
        severity="required",
        run=_signing_key,
    ),
    Check(
        id="secrets.bridge_sso",
        title="The bridge SSO secret is set",
        category="secrets",
        severity="required",
        run=_bridge_sso,
    ),
)
