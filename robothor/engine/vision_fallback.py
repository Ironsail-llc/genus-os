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

import base64
import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.pooled_completion import acompletion as pooled_acompletion

logger = logging.getLogger(__name__)

__all__ = [
    "PROVENANCE",
    "PROVENANCE_NOTE",
    "Backend",
    "Description",
    "NoVisionBackendError",
    "configured_local_model",
    "configured_remote_model",
    "describe_with_fallback",
    "price",
    "remote_answer",
    "resolve_backend",
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

#: Seconds a remote fallback gets for one picture. ``view_image`` is a single
#: image and the agent is waiting on it, so this is tighter than the batch's
#: per-image timeout, which is amortised over a fan-out.
REMOTE_TIMEOUT_SECONDS = 90.0

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


async def describe_with_fallback(
    data: bytes,
    mime: str,
    prompt: str,
    *,
    detail: str = "high",
    timeout: float = REMOTE_TIMEOUT_SECONDS,
) -> Description:
    """A description of *data* from whichever vision model is reachable.

    The ``view_image`` ladder, for a primary model that cannot take pictures:
    the local VLM first (this box's GPU, free), then the configured remote
    model. Raises :class:`NoVisionBackend` carrying one reason per rung when
    neither answers — a description nobody produced is the one thing this must
    never invent.

    ``detail`` defaults to ``high`` rather than the batch's ``low``: this is
    the tool for the single image somebody has to study, so the cheapness that
    makes a 200-image fan-out affordable is the wrong trade here.
    """
    reasons: list[str] = []

    from robothor.engine.tools.handlers.images import describe_image_bytes

    try:
        text = (await describe_image_bytes(data, prompt)).strip()
    except Exception as exc:  # noqa: BLE001 - every rung's failure is reported, not raised
        reasons.append(f"the local vision model is unavailable ({type(exc).__name__})")
        logger.debug("local vision rung failed: %s", exc)
    else:
        if text:
            return Description(text, "local", configured_local_model())
        reasons.append("the local vision model returned nothing")

    remote = configured_remote_model()
    refusal = _remote_refusal(remote)
    if refusal:
        reasons.append(refusal)
        raise NoVisionBackendError(reasons)

    try:
        text, tokens, cost = await remote_answer(
            Backend("remote", remote), data, mime, prompt, detail, timeout
        )
    except Exception as exc:  # noqa: BLE001 - same rule: named, never invented
        reasons.append(f"the remote vision model ({remote}) failed ({type(exc).__name__})")
        logger.warning("remote vision rung failed for %s: %s", remote, exc)
        raise NoVisionBackendError(reasons) from exc
    return Description(text, "remote", remote, tokens, cost)
