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

The one other way through is credential intake: an owner or admin typing to
the default chat agent in a top-level human chat run. That is decided from the
run itself — the caller's ``user_role``, the agent id and the live session's
trigger and parent — never from what the message says. A refusal under intake names the gate that held
and what opens it, so the agent can tell the person instead of guessing.

Once ``vault_set`` holds a value, the redactor knows it: every later
``redact`` replaces the exact characters with the fingerprint, which is what
takes a key with no recognisable shape out of the history, ``chat_messages``
and the run's task text.
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


#: This handler takes a credential from a person in chat; capability copy reads it.
CREDENTIAL_INTAKE = True

#: The human roles that may hand the chat agent a credential. The same pair
#: every operator surface admits (``crm/bridge/routers/_operator.py``).
INTAKE_ROLES = frozenset({"owner", "admin"})


def _default_chat_agent() -> str:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().default_chat_agent


def _live_session(ctx: ToolContext) -> Any:
    from robothor.engine.session_registry import lookup

    run_id = getattr(ctx, "run_id", "") or ""
    return lookup(run_id) if run_id else None


def _intake_triggers() -> frozenset[Any]:
    """The chat surfaces a person types a credential into.

    ``HUMAN_AUTHORED_TRIGGERS`` minus ``MANUAL``: that one is the CLI, but also
    the API trigger and resume endpoints and the unattended repair-resume of
    the chat agent, which runs as ``owner`` with nobody typing.
    """
    from robothor.engine.chat_backstop import HUMAN_AUTHORED_TRIGGERS
    from robothor.engine.models import TriggerType

    return HUMAN_AUTHORED_TRIGGERS - {TriggerType.MANUAL}


def _is_top_level_human_chat(session: Any) -> bool:
    """A run a person started in chat, with no parent run above it."""
    if session is None:
        return False
    run = session.run
    return (
        run.trigger_type in _intake_triggers()
        and not run.parent_run_id
        and not getattr(run, "nesting_depth", 0)
    )


def _is_registered_person(identity: Any) -> bool:
    """A channel-verified identity with a registered person behind it.

    Telegram hands an UNREGISTERED sender in the operator's default chat the
    ``owner`` role with ``verified=False``; the legacy sender parse marks an
    identity verified from an id and a role alone. Neither is a person the
    instance knows, so neither may hand over a credential.
    """
    if identity is None or getattr(identity, "verified", False) is not True:
        return False
    return bool(
        getattr(identity, "user_account_id", None) or getattr(identity, "tenant_user_id", None)
    )


def _intake_gate(ctx: ToolContext) -> str | None:
    """The first credential-intake gate this call fails, or None if it passes.

    ``chat_agent`` — this is not the default chat agent, so only the manifest
    tier opens it; ``role`` — the human is not an owner or admin;
    ``unverified_sender`` — the channel did not verify the sender as a
    registered person (see :func:`_is_registered_person`); ``human_chat`` —
    the run is not a top-level run a person started (a sub-agent, a cron, hook
    or workflow run, or no live run at all). Read from the run and the
    channel-resolved identity, never from message text.
    """
    if (getattr(ctx, "agent_id", "") or "") != _default_chat_agent():
        return "chat_agent"
    if (getattr(ctx, "user_role", "") or "") not in INTAKE_ROLES:
        return "role"
    identity = getattr(ctx, "identity", None)
    if not _is_registered_person(identity):
        return "unverified_sender"
    if (getattr(identity, "role", "") or "") not in INTAKE_ROLES:
        return "role"
    if not _is_top_level_human_chat(_live_session(ctx)):
        return "human_chat"
    return None


def _gated_refusal(ctx: ToolContext, tool: str, gate: str) -> dict[str, Any]:
    """The refusal under credential intake: which gate held, and what opens it."""
    role = getattr(ctx, "user_role", "") or ""
    if gate == "role":
        error = (
            f"{tool} is refused: only an owner or admin of this instance may hand this "
            f"agent a credential, and this conversation's role is {role or '(none)'!r}."
        )
        unlock = (
            "An owner or admin sends the credential to the agent in chat, or an "
            "operator stores it with `genus vault set <key>`."
        )
    elif gate == "unverified_sender":
        error = (
            f"{tool} is refused: the sender is not a registered person this channel "
            "verified, whatever role the conversation carries."
        )
        unlock = (
            "An operator registers the sender with `genus user add --name <name> "
            "--role owner --telegram-id <id>` (or the matching channel identifier), "
            "and they send the credential again."
        )
    elif gate in ("not_handed_over", "not_requested"):
        from robothor.engine.tools.handlers.vault_intake import request_refusal

        error, unlock = request_refusal(tool, gate)
    elif gate == "chat_agent":
        error = (
            f"{tool} is refused: a credential handed over in chat is kept by the "
            f"default chat agent ({_default_chat_agent()!r}), not by "
            f"{getattr(ctx, 'agent_id', '') or '(unattributed)'!r}, whose manifest does "
            f"not declare `{CREDENTIAL_TIER_KEY}: {OPERATOR_TIER}` under `v2:`."
        )
        unlock = (
            "Hand it to the default chat agent, or an operator adds "
            f"`{CREDENTIAL_TIER_KEY}: {OPERATOR_TIER}` under `v2:` in this agent's manifest."
        )
    else:
        error = (
            f"{tool} is refused: a credential is accepted only in a conversation a "
            "person started directly, not in a scheduled, hook, workflow or sub-agent run."
        )
        unlock = "The owner or an admin sends the credential to the default chat agent in chat."
    return {"error": error, "denied_by": "credential_tier", "gate": gate, "unlock": unlock}


def _operator_denial(
    ctx: ToolContext, tool: str, args: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """``None`` when this caller may use the vault tools, else the refusal.

    Fails closed on every uncertainty: no agent id, no manifest, an unreadable
    manifest directory, a tier that is not ``operator`` with a caller that does
    not pass every credential-intake gate. The alternative —
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
    gate = _intake_gate(ctx) or _request_gate(ctx, tool, args or {})
    return None if gate is None else _gated_refusal(ctx, tool, gate)


def _request_gate(ctx: ToolContext, tool: str, args: dict[str, Any]) -> str | None:
    """What the person's own message this turn asked for, against ``tool``."""
    from robothor.engine.tools.handlers.vault_intake import request_gate

    session = _live_session(ctx)
    message = str(getattr(session, "originating_message", "") or "")
    return request_gate(tool, args, message)


async def _denial(
    ctx: ToolContext, tool: str, args: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """:func:`_operator_denial`, off the event loop.

    It reads the agent's manifest from disk, which would stall every other run
    on the loop thread.
    """
    return await asyncio.to_thread(_operator_denial, ctx, tool, args)


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
    denial = await _denial(ctx, "vault_get", args)
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
    meta = await asyncio.to_thread(_metadata, key, getattr(ctx, "tenant_id", "") or "")
    return {
        "key": key,
        "configured": status.configured,
        "fingerprint": status.fingerprint,
        "source": status.source,
        "updated_at": status.updated_at,
        "stored_by": meta.get("stored_by"),
        "via": meta.get("via"),
        "last_resolved_at": _last_resolved_iso(key, getattr(ctx, "tenant_id", "") or ""),
    }


def _metadata(key: str, tenant_id: str) -> dict[str, Any]:
    """The row's audit metadata, or {} when there is none or it cannot be read."""
    import robothor.vault as vault

    kwargs: dict[str, Any] = {"tenant_id": tenant_id} if tenant_id else {}
    try:
        return dict(vault.get_metadata(key, **kwargs) or {})
    except Exception as exc:  # noqa: BLE001 - a status answer without its audit half
        logger.warning("vault_get: could not read the row's metadata (%s)", type(exc).__name__)
        return {}


def _last_resolved_iso(key: str, tenant_id: str) -> str | None:
    """When a reader in this process was last served the credential, ISO-8601."""
    from datetime import UTC, datetime

    from robothor.constants import DEFAULT_TENANT
    from robothor.secrets import last_resolved_at

    tenant = tenant_id or DEFAULT_TENANT
    stamps = [at for name in _reader_names(key) if (at := last_resolved_at(name, tenant_id=tenant))]
    return datetime.fromtimestamp(max(stamps), UTC).isoformat() if stamps else None


def _reader_names(key: str) -> tuple[str, ...]:
    """Every name a reader resolves this vault row by, plus its exported name."""
    from robothor.vault.naming import env_names_for_vault_key, normalise_key

    stored_at = normalise_key(key)
    return (*env_names_for_vault_key(stored_at), _resolve_env_name(stored_at))


def _forget_usage(key: str, tenant_id: str) -> None:
    """A written or deleted row has not been used yet: drop ITS stamps only."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.secrets import forget_last_resolved

    forget_last_resolved(*_reader_names(key), tenant_id=tenant_id or DEFAULT_TENANT)


@_handler("vault_set")
async def _vault_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Store a credential, and make it take effect now.

    The arguments of this call carry a credential, so the transcript redactor
    is what keeps it out of ``agent_run_steps.tool_input`` and the stored
    conversation. That is asserted in ``test_vault_set_is_redacted.py`` rather
    than assumed here.
    """
    denial = await _denial(ctx, "vault_set", args)
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
    session = _live_session(ctx)
    kwargs: dict[str, Any] = {
        "category": args.get("category", "credential"),
        "metadata": _audit_metadata(ctx, session),
    }
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    await asyncio.to_thread(vault.set, key, value, **kwargs)

    # The value is known now, so it can be taken out of the places the
    # person's message already reached — and of everything written after.
    stored_fingerprint = await asyncio.to_thread(fingerprint, value)
    await _forget_the_value_everywhere(value, stored_fingerprint, session)
    _forget_usage(key, tenant_id)

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
        "fingerprint": stored_fingerprint,
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


def _audit_metadata(ctx: ToolContext, session: Any) -> dict[str, Any]:
    """Who stored the row and how. Identifiers only, never the value."""
    if session is None:
        via = "tool"
    elif _is_top_level_human_chat(session):
        via = f"chat:{session.run.trigger_type}"
    else:
        via = f"run:{session.run.trigger_type}"
    return {
        "stored_by": getattr(ctx, "user_id", "") or None,
        "via": via,
        "run_id": getattr(ctx, "run_id", "") or None,
        "agent_id": getattr(ctx, "agent_id", "") or None,
    }


async def _forget_the_value_everywhere(value: str, stored_fingerprint: str, session: Any) -> None:
    """Register the value with the redactor and scrub the live run's task text.

    ``agent_runs.task_text`` is written at run START, before this call, so a
    shapeless key in the person's message is already in that row. History and
    ``chat_messages`` are written AFTER the run, through ``redact``, which now
    knows the value.
    """
    from robothor.secrets.redaction import redact, redact_known_value, register_known_value

    if not register_known_value(value, fingerprint=stored_fingerprint):
        return
    if session is None:
        return
    original = session.run.task_text
    cleaned = redact(original) if original else original
    originating = getattr(session, "originating_message", None)
    if originating:
        # Only THIS value: a second key in the same message is still the
        # person's hand-over, and the intake gate looks for it here.
        session.originating_message = redact_known_value(
            originating, value.strip(), f"<redacted:{stored_fingerprint}>"
        )
    if cleaned == original:
        return
    session.run.task_text = cleaned
    try:
        from robothor.engine import tracking

        await asyncio.to_thread(tracking.update_run, session.run.id, task_text=cleaned)
    except Exception as exc:  # noqa: BLE001 - the write succeeded; this is best effort
        logger.warning(
            "vault_set: stored the credential but could not rewrite the run's task text (%s)",
            type(exc).__name__,
        )


@_handler("vault_test")
async def _vault_test(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Prove a stored credential works, and say whose it is.

    The kind is inferred from the KEY, never passed in, so this cannot be used
    to run a vendor probe against an arbitrary value the caller supplies.
    """
    denial = await _denial(ctx, "vault_test", args)
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
    denial = await _denial(ctx, "vault_list", args)
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
    denial = await _denial(ctx, "vault_delete", args)
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
    _forget_usage(key, tenant_id)
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

        # Also clears the per-key vault cache: a reader holding a cached value
        # after a rotation is the incident again with a shorter fuse.
        await asyncio.to_thread(reset_vault_availability)
        await asyncio.to_thread(key_pool.reload_provider_keys)
    except Exception as exc:  # noqa: BLE001 - the write succeeded; this is best effort
        logger.warning(
            "vault: stored the credential but could not refresh cached readers (%s); "
            "it will be picked up on the next reload",
            type(exc).__name__,
        )
