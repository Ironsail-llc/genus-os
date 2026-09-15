"""The assistant's credential surface: keep it, prove it, never see it.

The operator handed the assistant a GitHub token over Telegram and expected it
to be kept, used and rotated by the assistant. Three things had to change for
that to be a reasonable thing to do.

``vault_get`` no longer returns the value. It returned the decrypted credential
straight to the model, which put every secret the instance owns one tool call
away from a transcript, a context window, and whatever the model said next.
What it returns instead is the write-only shape the Helm Secrets page already
uses — ``configured``, a ``fingerprint``, the ``source`` the accessor would
serve, and ``updated_at`` — which answers every question an assistant actually
has ("is it set?", "is it the one I just wrote?", "is the vault winning?")
without anybody reading a credential.

``vault_test`` is new, and it is what makes a rotation checkable. Before it,
proving a token worked meant using it and narrating the result, and proving
WHICH token was stored meant printing it.

``vault_set`` is unchanged in shape and now triggers an in-process reload, so a
rotation takes effect for cached readers immediately rather than at the next
restart — which the assistant cannot perform.

All of them need the operator credential tier — ``v2.credentials: operator``
in the agent's own manifest, a key nothing else interprets. It fails closed and
a spawned sub-agent cannot pass it: the child runs under its OWN agent id, so
its own manifest is what is read. There is deliberately no
``vault_rotate_from_message`` tool — the assistant simply calls ``vault_set``
with what it was handed, and the transcript redactor keeps the argument out of
the stored step.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: The manifest key that opens the vault tools, and the one value that does it.
#:
#: ``v2.credentials: operator``. Deliberately NOT the ``role:`` field, which is
#: what the first cut used and what made the prescribed opt-in an outage: that
#: field feeds ``resolve_service_role`` → ``check_tool_permission``, so setting
#: it to ``main`` — a role no ``role_permissions`` row seeds — denied ``main``
#: EVERY tool on a box running RBAC at enforce. An agent could not both keep
#: its tools and hold the vault tools. Two postures, two fields.
#:
#: Read from the DECLARED manifest, never from the resolved service role:
#: ``ROBOTHOR_DEFAULT_SERVICE_ROLE`` is a fleet-wide knob the SERVICE_ROLES
#: runbook tells operators to set, and reading through it made that knob a
#: one-line grant of ``vault_set`` to every sub-agent on the instance.
CREDENTIAL_TIER_KEY = "credentials"
OPERATOR_TIER = "operator"


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@lru_cache(maxsize=256)
def credential_tier(agent_id: str) -> str:
    """The tier ``agent_id``'s own manifest DECLARES, or "" if it declares none.

    Reads the raw manifest rather than ``AgentConfig``, for one reason that
    matters: ``AgentConfig`` fields can be filled in by fleet-wide defaults,
    and a credential grant that a fleet-wide knob can supply is not a grant
    anybody made. This reads what the file says.

    A seam as much as a lookup: the suite replaces it, so these handlers can be
    exercised without a manifest directory. Raising is meaningful — the caller
    turns it into a refusal, because an unreadable manifest is not evidence of
    an operator.
    """
    if not agent_id:
        return ""
    from pathlib import Path

    from robothor.engine.config import EngineConfig, load_manifest

    manifest = load_manifest(Path(EngineConfig.from_env().manifest_dir) / f"{agent_id}.yaml")
    if not isinstance(manifest, dict):
        return ""
    v2 = manifest.get("v2")
    if not isinstance(v2, dict):
        return ""
    return str(v2.get(CREDENTIAL_TIER_KEY, "") or "").strip().lower()


def _operator_denial(ctx: ToolContext, tool: str) -> dict[str, Any] | None:
    """``None`` when this caller may use the vault tools, else the refusal.

    Fails closed on every uncertainty: no agent id, no manifest, an unreadable
    manifest directory, a tier that is not ``operator``. The alternative —
    allowing when we cannot tell — is how a sub-agent ends up holding the
    instance's credentials. A spawned sub-agent runs under its OWN agent id, so
    its own manifest is what is read here; that is the sub-agent refusal, and
    it is a property of where the answer comes from rather than a check
    somebody has to remember.
    """
    agent_id = getattr(ctx, "agent_id", "") or ""
    try:
        tier = credential_tier(agent_id)
    except Exception as exc:  # noqa: BLE001 - uncertainty is a refusal, not a crash
        logger.warning(
            "vault tools: refusing %s for agent %s — its manifest could not be read (%s)",
            tool,
            agent_id or "<unattributed>",
            type(exc).__name__,
        )
        tier = ""
    if tier == OPERATOR_TIER:
        return None
    return {
        "error": (
            f"{tool} needs the operator credential tier and this agent does not have "
            f"it (its manifest declares {tier or '(none)'!r}). An operator grants it "
            f"by adding `{CREDENTIAL_TIER_KEY}: {OPERATOR_TIER}` under `v2:` in that "
            "agent's manifest. Credentials are otherwise handled by the operator's "
            "own agent: ask it to store, test or rotate this one, or use a tool that "
            "holds the credential for you."
        ),
        "denied_by": "credential_tier",
    }


def _resolve_env_name(key: str) -> str:
    """The environment name this vault row exports to, for the source lookup."""
    from robothor.vault.naming import env_name

    return env_name(key)


@_handler("vault_get")
async def _vault_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Whether a credential is configured, which one, and which store wins.

    Never the value. If the model needs the credential in order to DO
    something, the thing it needs is a tool that holds the credential itself,
    or a ``secrets:`` grant in its manifest — not the credential in its context
    window.
    """
    denial = _operator_denial(ctx, "vault_get")
    if denial:
        return denial

    key = args["key"]
    denial = _refuse_bootstrap(key, "read")
    if denial:
        return denial

    from robothor.secrets.status import status_for_key

    status = await asyncio.to_thread(
        status_for_key, key, tenant_id=getattr(ctx, "tenant_id", "") or ""
    )
    return {
        "key": key,
        "configured": status.configured,
        "fingerprint": status.fingerprint,
        "source": status.source,
        "updated_at": status.updated_at,
    }


@_handler("vault_set")
async def _vault_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Store a credential, and make it take effect now.

    The arguments of this call carry a credential, so the transcript redactor
    is what keeps it out of ``agent_run_steps.tool_input`` and the stored
    conversation. That is asserted in ``test_vault_set_is_redacted.py`` rather
    than assumed here.
    """
    denial = _operator_denial(ctx, "vault_set")
    if denial:
        return denial

    key = args["key"]
    value = args["value"]
    denial = _refuse_bootstrap(key, "write")
    if denial:
        return denial

    import robothor.vault as vault
    from robothor.secrets.fingerprint import fingerprint

    tenant_id = getattr(ctx, "tenant_id", "") or ""
    kwargs: dict[str, Any] = {"category": args.get("category", "credential")}
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    await asyncio.to_thread(vault.set, key, value, **kwargs)

    # A rotation the assistant cannot make take effect is not a rotation it can
    # perform: key_pool caches provider keys and republishes them into the
    # process environment for litellm, so without this the write would be
    # correct and invisible until a restart -- which the assistant must never
    # need to ask for.
    await _reload_cached_readers()

    from robothor.vault.naming import env_names_for_vault_key, normalise_key

    stored_at = normalise_key(key)
    readers = env_names_for_vault_key(stored_at)
    answer: dict[str, Any] = {
        "success": True,
        "key": stored_at,
        "fingerprint": fingerprint(value),
        "readable_as": list(readers),
    }
    if not readers:
        # The row is real, encrypted and stored — and nothing in the platform
        # will ever look at it. Silence here is the incident with a tool
        # reporting success, so say it plainly and name a key that works.
        answer["warning"] = (
            f"stored, but no reader looks at {stored_at!r}: no environment variable "
            "resolves to it, so the platform will not use this credential. Store it "
            "under the variable name its reader uses (lower-cased), e.g. "
            "`providers/<vendor>/api_key` for a provider or API token, "
            "`channels/<channel>/<field>` for a channel setting."
        )
    return answer


@_handler("vault_test")
async def _vault_test(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Prove a stored credential works, and say whose it is.

    The kind is inferred from the KEY, never passed in, so this cannot be used
    to run a vendor probe against an arbitrary value the caller supplies.
    """
    denial = _operator_denial(ctx, "vault_test")
    if denial:
        return denial

    key = args["key"]
    denial = _refuse_bootstrap(key, "test")
    if denial:
        return denial

    from robothor.secrets import testers

    kind = testers.kind_for_key(key)
    if kind is None:
        return {
            "ok": False,
            "identity_hint": None,
            "error_class": "unknown_kind",
            "detail": (
                f"No credential tester knows how to probe {key!r}. Testable kinds: "
                f"{', '.join(sorted(set(testers._KINDS.values())))}."
            ),
        }

    from robothor.secrets.status import resolve_for_key

    value = await asyncio.to_thread(
        resolve_for_key, key, tenant_id=getattr(ctx, "tenant_id", "") or ""
    )
    if value is None:
        return {"ok": False, "identity_hint": None, "error_class": "not_configured"}

    outcome = await testers.probe(kind, value)
    return {
        "ok": outcome.ok,
        "identity_hint": _safe_hint(outcome.identity_hint, value),
        "error_class": outcome.error_class,
        "kind": kind,
    }


#: The shortest run of the credential that, appearing in a hint, means the hint
#: is carrying key material. Short enough to catch a deliberate prefix, long
#: enough that an ordinary word shared by chance is not a match.
_HINT_OVERLAP_CHARS = 8


def _safe_hint(hint: str | None, value: str) -> str | None:
    """The identity hint, unless it is a way of reading the credential back.

    ``identity_hint`` is whatever the vendor calls the account, and for
    OpenRouter that is ``data.label`` — chosen by the key's OWNER, and commonly
    set to a fragment of the key. So the one field this tool returns out of a
    vendor's response body is a channel back to the value, and a probe found it
    returning the key verbatim.

    Two gates. The redactor, which catches a hint that is a recognisable
    credential in its own right; and a substring check against the value we
    dialled with, which catches the case the redactor cannot know about — a
    label that carries part of THIS key inside an otherwise innocent string.
    """
    if not hint:
        return None
    from robothor.secrets.redaction import PLACEHOLDER, redact

    cleaned = redact(hint)
    if cleaned != hint:
        return PLACEHOLDER
    if value:
        for start in range(0, max(1, len(value) - _HINT_OVERLAP_CHARS + 1)):
            if value[start : start + _HINT_OVERLAP_CHARS] in cleaned:
                logger.warning(
                    "vault_test: the vendor's identity hint carried part of the "
                    "credential; reporting it as redacted"
                )
                return PLACEHOLDER
    return cleaned


@_handler("vault_list")
async def _vault_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    denial = _operator_denial(ctx, "vault_list")
    if denial:
        return denial

    import robothor.vault as vault

    kwargs: dict[str, Any] = {"category": args.get("category")}
    tenant_id = getattr(ctx, "tenant_id", "") or ""
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    keys = await asyncio.to_thread(vault.list, **kwargs)
    return {"keys": keys, "count": len(keys)}


@_handler("vault_delete")
async def _vault_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    denial = _operator_denial(ctx, "vault_delete")
    if denial:
        return denial

    key = args["key"]
    denial = _refuse_bootstrap(key, "delete")
    if denial:
        return denial

    import robothor.vault as vault

    kwargs: dict[str, Any] = {}
    tenant_id = getattr(ctx, "tenant_id", "") or ""
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    deleted = await asyncio.to_thread(vault.delete, key, **kwargs)
    if not deleted:
        return {"success": False, "key": key}

    await _reload_cached_readers()
    answer: dict[str, Any] = {"success": True, "key": key}

    # Deleting the vault row does not delete the credential: the accessor falls
    # through to the environment, so a reader that was being served the vault's
    # value now gets whatever the box booted with — which may be the dead one
    # this row replaced. A delete that silently restores a stale credential is
    # the incident running backwards.
    from robothor.secrets.status import status_for_key

    after = await asyncio.to_thread(
        status_for_key, key, tenant_id=getattr(ctx, "tenant_id", "") or ""
    )
    if after.configured and after.source == "env":
        answer["warning"] = (
            f"the vault row is gone, but the environment still holds a value for "
            f"{after.name} ({after.fingerprint}), and readers are now served THAT. "
            "If you meant to remove the credential entirely, it also has to come out "
            "of the instance's secrets file."
        )
    return answer


def _refuse_bootstrap(key: str, verb: str) -> dict[str, Any] | None:
    """Keep the tools away from the credentials that bring the instance up.

    The database password opens every tenant's data AND the vault's own rows;
    the signing keys mint sessions and decrypt every stored MFA secret. None of
    them is a credential the assistant was handed, none is one it can rotate
    safely, and a fingerprint of one is a fact about the instance's identity
    that a tool result has no reason to carry.
    """
    from robothor.secrets.classification import is_bootstrap
    from robothor.vault.naming import env_name, normalise_key

    # Normalised first: `'ROBOTHOR_DB_PASSWORD '` compared as a padded string
    # is not the bootstrap name, and slipped straight past this refusal.
    if not is_bootstrap(env_name(normalise_key(str(key)))):
        return None
    return {
        "error": (
            f"{key!r} is a bootstrap credential: it is what brings this instance up, "
            f"so the vault tools will not {verb} it. Bootstrap credentials live in "
            "the instance's secrets file and are changed by its operator, not by an "
            "agent."
        ),
        "denied_by": "bootstrap",
    }


async def _reload_cached_readers() -> None:
    """Pick up a vault change in-process, with no HTTP call to ourselves.

    Deliberately not a request to ``POST /api/admin/secrets/reload``: an engine
    calling its own HTTP API needs the API to be up, needs credentials for it,
    and turns a local cache refresh into a network dependency.
    """
    try:
        from robothor.engine import key_pool
        from robothor.secrets import reset_vault_availability

        await asyncio.to_thread(reset_vault_availability)
        await asyncio.to_thread(key_pool.reload_provider_keys)
    except Exception as exc:  # noqa: BLE001 - the write succeeded; this is best effort
        logger.warning(
            "vault: stored the credential but could not refresh cached readers (%s); "
            "it will be picked up on the next reload",
            type(exc).__name__,
        )
