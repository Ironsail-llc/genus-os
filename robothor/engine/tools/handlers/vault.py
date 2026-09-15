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

All four are operator-tier. The gate is the calling agent's own manifest
``role``, which fails closed and which a spawned sub-agent cannot pass: the
child runs under its OWN agent id, so its own manifest is what is read, and a
worker that declares no role resolves to ``service``. There is deliberately no
``vault_rotate_from_message`` tool — the assistant simply calls ``vault_set``
with what it was handed, and the transcript redactor keeps the argument out of
the stored step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Manifest ``role`` values that may touch credentials. The operator's own
#: agent and the delivery agents that speak for them — nothing else. ``service``
#: is deliberately absent even though migration 107 seeds it as ('*', 'allow'):
#: that seed is what makes the DB-backed RBAC gate a no-op today, so a control
#: that leaned on it would be a control in name only (the failure
#: ``service_roles.py`` was written to make visible).
OPERATOR_ROLES = frozenset({"main", "owner", "operator", "admin"})


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def manifest_role(agent_id: str, workspace: str = "") -> str:
    """The ``role`` this agent's own manifest declares, or "" if it declares none.

    A seam as much as a lookup: the suite replaces it, so these handlers can be
    exercised without a manifest directory. Raising is meaningful — the caller
    turns it into a refusal, because an unreadable manifest is not evidence of
    an operator.
    """
    from pathlib import Path

    from robothor.engine.config import EngineConfig, load_agent_config

    if not agent_id:
        return ""
    config = load_agent_config(agent_id, Path(EngineConfig.from_env().manifest_dir), workspace=None)
    if config is None:
        return ""
    return str(getattr(config, "service_role", "") or "").strip().lower()


def _operator_denial(ctx: ToolContext, tool: str) -> dict[str, Any] | None:
    """``None`` when this caller may use the vault tools, else the refusal.

    Fails closed on every uncertainty: no agent id, no manifest, an unreadable
    manifest directory, a role that is not operator-tier. The alternative —
    allowing when we cannot tell — is how a sub-agent ends up holding the
    instance's credentials.
    """
    agent_id = getattr(ctx, "agent_id", "") or ""
    try:
        role = manifest_role(agent_id, getattr(ctx, "workspace", "") or "")
    except Exception as exc:  # noqa: BLE001 - uncertainty is a refusal, not a crash
        logger.warning(
            "vault tools: refusing %s for agent %s — its manifest could not be read (%s)",
            tool,
            agent_id or "<unattributed>",
            type(exc).__name__,
        )
        role = ""
    if role in OPERATOR_ROLES:
        return None
    return {
        "error": (
            f"{tool} is operator-tier and this agent is not: its manifest declares "
            f"role {role or '(none)'!r}, not one of {sorted(OPERATOR_ROLES)}. "
            "Credentials are handled by the operator's own agent. Ask it to store, "
            "test or rotate the credential, or use it through a tool that holds it "
            "for you."
        ),
        "denied_by": "operator_tier",
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

    return {"success": True, "key": key, "fingerprint": fingerprint(value)}


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
        "identity_hint": outcome.identity_hint,
        "error_class": outcome.error_class,
        "kind": kind,
    }


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
    if deleted:
        await _reload_cached_readers()
    return {"success": deleted, "key": key}


def _refuse_bootstrap(key: str, verb: str) -> dict[str, Any] | None:
    """Keep the tools away from the credentials that bring the instance up.

    The database password opens every tenant's data AND the vault's own rows;
    the signing keys mint sessions and decrypt every stored MFA secret. None of
    them is a credential the assistant was handed, none is one it can rotate
    safely, and a fingerprint of one is a fact about the instance's identity
    that a tool result has no reason to carry.
    """
    from robothor.secrets.classification import is_bootstrap
    from robothor.vault.naming import env_name

    if not is_bootstrap(env_name(str(key))):
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
