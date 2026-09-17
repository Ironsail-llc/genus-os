"""Which model looks at a picture, and what it costs to ask.

Extracted from :mod:`robothor.engine.vision_batch` on 2026-09-17, because two
tools needed the same answer and only one of them had it.

``analyze_image`` shipped with a ladder: a remote vision model the registry
declares able to accept images, else this box's Ollama VLM, else an honest
refusal. ``view_image`` had half of one — the primary model, then the local
VLM, then "nobody looked". In a container with no GPU that second rung is
always down, so every ``view_image`` call in the measured 2026-09-17 run
returned::

    openrouter/z-ai/glm-5.2 cannot accept images and the local vision model
    is unavailable (ConnectError)

on an instance whose ``ROBOTHOR_VISION_REMOTE_MODEL`` was configured and
working — ``analyze_image`` was using it in the same run. The agent therefore
could not look at anything itself, and when the batch's answers disagreed with
the filenames it had no way to check which was right. It chose the names, and
scored 0 on a task the answers it already held were worth 0.94 on.

So the rungs live here, once, and both tools climb them. The ORDER differs and
that is deliberate, not drift:

* ``analyze_image`` prefers the remote model. It is about to make up to 200
  calls, the operator named a vision backend for exactly this, and a local
  27B-class VLM on one GPU serialises.
* ``view_image`` prefers the local model. One image, no money, and the local
  VLM is already the thing ``describe_image_bytes`` exists to dial.

Both end the same way: a rung that failed is NAMED. "The remote model timed
out" and "you configured no remote model" need different fixes, and a tool
that collapses them into "unavailable" is the reason the measured run believed
it had no vision capability at all.

Provenance
----------
The other half of the same failure. A model reading ``answer: "natural
scene"`` for a file called ``3d_architectural_render.jpg`` has nothing telling
it that the answer came from the pixels and the name was never consulted — so
a confident-looking filename wins against what reads as a bare token. Both
tools now carry :data:`PROVENANCE` and :data:`PROVENANCE_NOTE`, in the result
and in the tool description, and the general rule that an observation outranks
a name lives in ``prompts.py`` as its own numbered rule.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robothor.engine.pooled_completion import acompletion as pooled_acompletion

logger = logging.getLogger(__name__)

__all__ = [
    "NO_WORKSPACE_REFUSAL",
    "PROVENANCE",
    "PROVENANCE_NOTE",
    "Backend",
    "Description",
    "NoVisionBackendError",
    "configured_local_model",
    "configured_remote_model",
    "describe_with_fallback",
    "look_timeout",
    "path_refusal",
    "price",
    "remote_answer",
    "resolve_backend",
    "safe_backend_message",
    "workspace_root",
]

#: What a vision result's ``provenance`` field says. A machine-readable token
#: rather than a sentence, so a step ledger, a spilled table and a downstream
#: check can agree on it; :data:`PROVENANCE_NOTE` is the sentence that goes
#: beside it for whoever is reading.
PROVENANCE = "image-content"

#: The one line every vision result carries. Written as a statement of fact
#: about how the answer was produced, because the failure it addresses is an
#: agent inferring — wrongly — that a tool which returns a label about a file
#: must have read the file's NAME.
PROVENANCE_NOTE = (
    "answers come from the model looking at the image content; the filename was not consulted"
)

#: Answer tokens one image gets. Enough for a paragraph of description or a
#: transcription; short enough that 200 of them do not become a document.
MAX_ANSWER_TOKENS = 1024

#: Seconds ONE rung of the ``view_image`` ladder gets when the setting cannot
#: be read. Two rungs at this budget, plus overhead, must fit inside the
#: agent's ``tool_timeout_seconds`` (120 by default) — see
#: :func:`describe_with_fallback` for what happens when they do not.
DEFAULT_LOOK_TIMEOUT_SECONDS = 45.0

#: How much of a failed rung's own message is quoted back. Enough to tell a
#: 402 from an expired key from a DNS failure; short enough that a provider
#: answering with a page of HTML does not put the page in the context.
MAX_RUNG_MESSAGE_CHARS = 200

#: What the vision model is told it is for. The literal, insistent form
#: ``view_image`` uses — a model that decides it "cannot access websites"
#: because the screenshot contains a URL has described nothing — with the
#: batch's own instruction to answer the question that was asked.
VISION_SYSTEM_PROMPT = (
    "You are a vision system answering one question about one image. Answer the question "
    "directly and concretely, in at most a short paragraph. Read and transcribe any text "
    "that bears on the answer, exactly as shown. Never refuse, never say you cannot access "
    "websites or URLs — a URL in an image is text to be read, not a page to visit. If the "
    "image does not contain what was asked about, say so plainly rather than guessing."
)


@dataclass(frozen=True)
class Backend:
    """Which vision model answers, and how it is dialled.

    ``kind`` is ``"remote"`` (an OpenAI-compatible provider, images as data
    URIs) or ``"local"`` (this box's Ollama VLM). Two code paths, one because
    the sandbox has no Ollama and one because the box should not have to pay a
    provider to look at its own screenshots.

    ``note`` is how a fallback tells the truth. A batch that quietly answered
    from a different model than the operator configured is a result the agent
    cannot reason about, so the sentence rides on the backend and lands in the
    result.
    """

    kind: str
    model: str
    note: str = ""


@dataclass(frozen=True)
class Description:
    """One picture described by something other than the agent's own model.

    ``backend`` and ``model`` say who looked; ``tokens`` and ``cost_usd`` are
    zero on the local rung because that is this box's GPU, and the run's spend
    must not gain a number nobody paid.
    """

    text: str
    backend: str
    model: str
    tokens: int = 0
    cost_usd: float = 0.0


class NoVisionBackendError(RuntimeError):
    """Nothing looked, and here is what each rung said.

    Carries the per-rung reasons rather than one flattened sentence: the
    measured failure was an agent reading "the local vision model is
    unavailable" and concluding the instance had no vision at all, when a
    configured remote model was one rung further up.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons) or "no vision backend is available")


def _settings() -> Any:
    from robothor.settings import get_settings

    return get_settings()


def configured_remote_model() -> str:
    """``ROBOTHOR_VISION_REMOTE_MODEL``, or empty when the box uses Ollama."""
    try:
        return str(_settings().providers.vision_remote_model or "").strip()
    except Exception:  # noqa: BLE001 - a missing config is "not configured", not a crash
        logger.debug("settings unavailable while resolving the remote vision model")
        return ""


def configured_local_model() -> str:
    """``ROBOTHOR_VISION_MODEL`` — the same field ``view_image`` reads."""
    try:
        return str(_settings().ollama.vision_model or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("settings unavailable while resolving the local vision model")
        return ""


def look_timeout() -> float:
    """``ROBOTHOR_VISION_LOOK_TIMEOUT`` — the budget for ONE rung of the ladder."""
    try:
        configured = float(_settings().providers.vision_look_timeout)
    except Exception:  # noqa: BLE001
        return DEFAULT_LOOK_TIMEOUT_SECONDS
    return configured if configured > 0 else DEFAULT_LOOK_TIMEOUT_SECONDS


def workspace_root(workspace: str) -> Path | None:
    """The tree an image has to be inside, or None when there is no answer.

    The caller's ``ctx.workspace`` first, settings second — the idiom
    ``tools/handlers/attachments.py`` uses for the same question, and for the
    same reason: an unresolvable workspace refuses rather than defaulting to
    "anywhere on the filesystem".
    """
    if workspace:
        return Path(workspace).expanduser().resolve(strict=False)
    try:
        from robothor.settings.sources import workspace_path

        resolved = workspace_path()
        return resolved.resolve(strict=False) if resolved is not None else None
    except Exception:  # noqa: BLE001 - an unresolvable workspace refuses, never reads
        return None


def path_refusal(resolved: Path, root: Path, display_name: str) -> str:
    """Why no vision tool may read *resolved*, or ``""`` when it may.

    The ONE place both image tools ask, which is the point. Round-1 review I-4
    found ``view_image`` asking neither question while ``analyze_image`` asked
    both of them about the same bytes: a file under the instance's secrets
    directory, and a file outside every workspace, were base64'd and posted to
    a third-party provider. Two tools with two ideas of what a vision tool may
    read is how one of them ends up being the way round the other.

    Order matters and mirrors ``attachment_gate.resolve_for_send``: containment
    on the RESOLVED path, so a symlink pointing out of the tree is caught, then
    the secret-path NAME rule before any stat, so a refusal never reveals
    whether the file exists.
    """
    if root != resolved and root not in resolved.parents:
        return (
            f"refused: {display_name} resolves outside the workspace. Only images "
            "inside the workspace can be read."
        )
    from robothor.engine.secret_paths import is_secret_path, refusal_for

    if is_secret_path(resolved):
        return refusal_for(resolved)
    return ""


#: What every vision tool answers when it cannot tell what "inside the
#: workspace" means. Shared so the two tools refuse in the same words.
NO_WORKSPACE_REFUSAL = (
    "refused: the workspace could not be resolved, so containment cannot be "
    "judged. Set ROBOTHOR_WORKSPACE."
)


def _remote_refusal(remote: str) -> str:
    """Why a configured remote model is not being dialled, or "" when it is.

    The gate is ``accepts`` — not "not declared blind". The first version asked
    ``!= "rejects"``, which passes every model nobody has written an entry for,
    and a hostile review set the setting to an undeclared model and watched the
    batch dial it: OpenRouter answered ``404 No endpoints found that support
    image input`` per image. An operator naming a model to BE the vision
    backend has made a configuration mistake worth reporting, not a risk worth
    taking 200 times.
    """
    if not remote:
        return "no remote vision model is configured (ROBOTHOR_VISION_REMOTE_MODEL)"
    from robothor.engine.model_registry import image_capability

    capability = image_capability(remote)
    if capability == "accepts":
        return ""
    why = (
        "does not accept images"
        if capability == "rejects"
        else "is not declared `accepts_images` in the engine's model registry"
    )
    return f"the configured remote vision model ({remote}) {why}"


def resolve_backend() -> tuple[Backend | None, str]:
    """``(backend, refusal)`` — exactly one of the two is meaningful.

    The batch ladder: the declared remote model first (it is about to be asked
    up to 200 questions), the local VLM second, a refusal that names the
    setting to fix third.

    ``view_image`` is right to keep dialling its PRIMARY on ``unknown`` and
    this is right not to, for a reason worth stating: there the model is
    whatever the fleet happens to be running and one image is at stake, so the
    result carries a note and the agent learns. Here an operator has explicitly
    named a model to be *the vision backend*.
    """
    remote = configured_remote_model()
    local = configured_local_model()
    refusal = _remote_refusal(remote)
    if remote and not refusal:
        return Backend("remote", remote), ""
    if remote and not local:
        return None, (
            f"refused: {refusal}. Add a registry entry declaring "
            "`accepts_images=True` for it, name a declared model in "
            "ROBOTHOR_VISION_REMOTE_MODEL, or configure a local one with "
            "ROBOTHOR_VISION_MODEL."
        )
    if remote:
        logger.warning("remote vision model %s is unusable; falling back to the local VLM", remote)
        return (
            Backend(
                "local",
                local,
                note=f"{refusal}, so the local vision model ({local}) answered instead.",
            ),
            "",
        )
    if local:
        return Backend("local", local), ""
    return None, (
        "refused: no vision model is configured. Set ROBOTHOR_VISION_MODEL (local, via "
        "Ollama) or ROBOTHOR_VISION_REMOTE_MODEL (a provider model the registry declares "
        "`accepts_images`)."
    )


def price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """What this call cost, from the registry's declared rates.

    Computed here rather than read from the provider's response: the registry
    is what every other cost number on this instance is derived from, and a
    tool result that reported a different basis would make the run total
    disagree with itself.
    """
    try:
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits(model)
    except Exception:  # noqa: BLE001 - an unpriced model costs an unknown amount, not a crash
        return 0.0
    return (
        prompt_tokens * limits.input_cost_per_token
        + completion_tokens * limits.output_cost_per_token
    )


async def remote_answer(
    backend: Backend, data: bytes, mime: str, question: str, detail: str, timeout: float
) -> tuple[str, int, float]:
    """``(answer, tokens, cost)`` from an OpenAI-compatible vision provider.

    Through the pooled client, never ``litellm.acompletion`` directly: a
    module that resolves its own key cannot rotate off a capped one, which is
    how a single retired credential kept being re-dialled for a whole day.
    """
    payload = base64.b64encode(data).decode("ascii")
    response = await pooled_acompletion(
        model=backend.model,
        messages=[
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{payload}", "detail": detail},
                    },
                ],
            },
        ],
        max_tokens=MAX_ANSWER_TOKENS,
        temperature=0.1,
        timeout=timeout,
    )
    answer = str(response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if not answer:
        raise RuntimeError("the vision model returned nothing")
    return (
        answer,
        prompt_tokens + completion_tokens,
        price(backend.model, prompt_tokens, completion_tokens),
    )


def _why(what: str, exc: BaseException) -> str:
    """One rung's failure: its type AND its message, bounded.

    Round-1 review I-2. The first cut reported ``type(exc).__name__`` alone, so
    an expired key, a 402 and a network partition all reached the agent as
    ``failed (APIError)`` — three different fixes behind one word. The message
    is what distinguishes them and it is the provider's own text, so it is
    quoted rather than pasted: a backend that answers a failure with a page of
    HTML must not put the page into the context under an error key.

    **Redacted first, then capped**, and the order is the point. Round-2 review
    C-1: a litellm/OpenRouter ``AuthenticationError`` carries the request
    headers and the ``api_key``, so a single 401 put this instance's key into
    the tool result, the model's context and ``agent_run_steps`` — nothing
    redacts a tool result that returns NORMALLY, which is why the precedent
    sits in ``dispatch.py``'s audit row and not in the tool layer. Redacting
    after the cap would be worse than useless: a 200-character cut can slice a
    token in half and leave a prefix no redactor still recognises.

    ``redact`` never raises and never returns None, so this cannot become the
    second bug in a line that is already reporting a failure.

    What IS interpolated from configuration is model NAMES — ``the local vision
    model (llava:7b)`` — which an operator needs and which are not secrets. No
    credential-bearing setting is read here at all.
    """
    from robothor.secrets.redaction import redact

    message = redact(" ".join(str(exc).split()))[:MAX_RUNG_MESSAGE_CHARS]
    return f"{what} ({type(exc).__name__}{': ' + message if message else ''})"


def safe_backend_message(exc: BaseException) -> str:
    """A backend exception as it may be SHOWN — redacted, collapsed, capped.

    The rule :func:`_why` applies, reachable by everything that repeats a
    backend's own words: the log lines here, and `vision_batch`'s failed rows.
    A log line outlives the run and is exported wholesale into a support
    bundle, and a spilled batch table is a FILE under the workspace that the
    tool tells the agent to open — so the rung that must not put a key in the
    context must not put one in the journal or on disk either.

    Public, and named rather than `_safe`, because a second module imports it:
    the batch half of this leak (round-2 re-check C-3) is the worse half, since
    a 401 fails every image and two hundred images are two hundred copies.
    """
    from robothor.secrets.redaction import redact

    return redact(" ".join(str(exc).split()))[:MAX_RUNG_MESSAGE_CHARS]


async def describe_with_fallback(
    data: bytes,
    mime: str,
    prompt: str,
    *,
    detail: str = "high",
    timeout: float | None = None,
) -> Description:
    """A description of *data* from whichever vision model is reachable.

    The ``view_image`` ladder, for a primary model that cannot take pictures:
    the local VLM first (this box's GPU, free), then the configured remote
    model. Raises :class:`NoVisionBackendError` carrying one reason per rung
    when neither answers — a description nobody produced is the one thing this
    must never invent.

    **Each rung is bounded separately** (:func:`look_timeout`), and that is the
    whole point of the number. Round-1 review I-1: the local rung used to take
    ``images.VISION_TIMEOUT_SECONDS`` (120 s) and the remote rung 90 s, against
    a ``view_image`` tool deadline of 120 s — so a box whose Ollama is up but
    SLOW, which is the ordinary case for a cold model on a busy GPU, spent the
    entire tool budget on the rung that was going to fail and was cancelled
    before the remote rung was tried. The agent then saw a tool timeout, which
    reads to it exactly like the "this instance has no vision" the ladder
    exists to abolish. Both rungs at 45 s fit inside 120 s with room to spare.

    ``detail`` defaults to ``high`` rather than the batch's ``low``: this is
    the tool for the single image somebody has to study, so the cheapness that
    makes a 200-image fan-out affordable is the wrong trade here.
    """
    reasons: list[str] = []
    budget = look_timeout() if timeout is None else timeout

    from robothor.engine.tools.handlers.images import describe_image_bytes

    local = configured_local_model()
    if not local:
        # Asked BEFORE dialling, because `describe_image_bytes` reports a
        # missing setting by raising — and a rung that turns "you configured
        # nothing" into "something went wrong" sends the operator looking for
        # an outage. This module's whole promise is that the two read
        # differently.
        reasons.append("no local vision model is configured (ROBOTHOR_VISION_MODEL)")
    else:
        try:
            # The budget is passed down AND enforced here. A backend that
            # ignores its own timeout kwarg is not hypothetical on this
            # instance (litellm did exactly that), and a rung that overruns is
            # the whole of I-1.
            async with asyncio.timeout(budget):
                text = (await describe_image_bytes(data, prompt, timeout=budget)).strip()
        except Exception as exc:  # noqa: BLE001 - every rung's failure is reported, not raised
            reasons.append(_why(f"the local vision model ({local}) is unavailable", exc))
            logger.debug("local vision rung failed: %s", safe_backend_message(exc))
        else:
            if text:
                return Description(text, "local", local)
            reasons.append(f"the local vision model ({local}) returned nothing")

    remote = configured_remote_model()
    refusal = _remote_refusal(remote)
    if refusal:
        reasons.append(refusal)
        raise NoVisionBackendError(reasons)

    try:
        async with asyncio.timeout(budget):
            text, tokens, cost = await remote_answer(
                Backend("remote", remote), data, mime, prompt, detail, budget
            )
    except Exception as exc:  # noqa: BLE001 - same rule: named, never invented
        reasons.append(_why(f"the remote vision model ({remote}) failed", exc))
        logger.warning("remote vision rung failed for %s: %s", remote, safe_backend_message(exc))
        raise NoVisionBackendError(reasons) from exc
    return Description(text, "remote", remote, tokens, cost)
