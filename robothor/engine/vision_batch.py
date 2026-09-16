"""One question, many pictures, answered out of band.

Measured 2026-09-16. On the WildClawBench Productivity task that hands an
agent a folder of photographs and asks it to categorise them, the competing
harness called its own vision tool a hundred times and scored 0.99; this
engine called ``view_image`` four times and scored 0.28 — near random on that
task's scale.

The difference is not the model, it is where the picture goes. ``view_image``
puts the image into the PRIMARY model's context (see
``tools/handlers/images.py``): one turn per image, the image tokens stay in
the conversation for the rest of the run, and the marginal cost of the tenth
look is paid on every call after it. An agent that has learned looking is
expensive stops looking. The competing tool is out of band — a separate
vision call per image, only the text answer returns — so it is cheap enough
to loop, and looping is what the task rewards.

So ``analyze_image`` takes a LIST of paths and ONE question, fans the vision
calls out with a bounded concurrency, and returns text. Nothing it returns
carries ``image_base64`` or ``image_mime``, which is the convention
``session.py`` keys its image-block emission on: an out-of-band tool that
quietly refilled the context with pictures would be ``view_image`` with extra
steps.

What it will not do
-------------------
* **Show a model a picture it cannot see.** The remote backend is checked
  against ``model_registry.image_capability`` first — the same honesty
  ``view_image`` learned in #578 — and falls back to the local VLM rather
  than posting an image block a provider will 404.
* **Read a file outside the workspace, or a credentials file.** Containment is
  judged on the RESOLVED path, so a symlink pointing out of the tree is
  refused; the secret-path rule is asked BEFORE the filesystem, so a refusal
  does not reveal whether the file exists.
* **Let one image take the batch down.** A backend that hangs, raises, or
  returns nothing marks that image and no other. A whole-call deadline bounds
  the case where the backend hangs for EVERY image: at 200 paths, four at a
  time, a per-image timeout alone would keep a dead backend alive for over an
  hour inside a single tool call.
* **Decode a 50 MB file to find out it was too big.** The size ceiling is a
  ``stat``, before Pillow is handed anything.

What containment here is, and is not
------------------------------------
It is a SCOPE, not an exfiltration boundary, and the difference is worth
stating because the rule looks like ``attachment_gate``'s and is not doing the
same job. There, a file's bytes leave the box to a person; here they reach a
provider only when a remote backend is configured, and the agent asking could
have copied the same file into the workspace with ``exec`` first. So this
refuses a path that wanders out of the tree — the honest reading of "analyze
the images in my workspace" — and does not pretend to be the control that
stops a determined agent reading a file it is already allowed to read. The
rung that matters is the secret-path one, which is shared with ``read_file``
and ``exec`` and is about credentials, not location.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robothor.engine.pooled_completion import acompletion as pooled_acompletion
from robothor.engine.tools.handlers.images import (
    UnsupportedImageError,
    describe_image_bytes,
    prepare_image_bytes,
)

logger = logging.getLogger(__name__)

#: The most images one call may take. Two hundred is what the measured
#: behaviour needs (the task that exposed this gap holds ~100 files) and a
#: bound the run can survive: at the default concurrency and timeout, a
#: completely dead backend still ends inside the batch deadline below.
MAX_PATHS = 200

#: Vision calls in flight at once when nobody says otherwise. Four is what a
#: single local VLM on one GPU serves without queueing into its own timeout;
#: a remote backend can be told to go wider per call.
DEFAULT_CONCURRENCY = 4

#: The ceiling on that, whatever an agent asks for. An agent that asks for 500
#: is not making a considered judgement about the provider's rate limit.
MAX_CONCURRENCY = 16

#: Seconds one image gets before it is marked timed out and the batch moves on.
DEFAULT_TIMEOUT_SECONDS = 90.0

#: Seconds the whole call gets. See the module docstring: the per-image timeout
#: alone does not bound a backend that hangs for every image.
DEFAULT_DEADLINE_SECONDS = 600.0

#: Biggest file this will decode. A provider will not accept it either, and
#: the point of checking is that a decompression bomb costs the same whether
#: the pixels are used or thrown away.
MAX_IMAGE_BYTES = 32 * 1024 * 1024

#: Longest question forwarded to the backend. A question is a question; a
#: novel pasted into this field is a way to pay for the primary model's
#: context in the vision model's.
MAX_QUESTION_CHARS = 2000

#: How much detail the provider is asked to render the image at. ``low`` by
#: default because the whole design is "cheap enough to call a hundred times";
#: the schema tells the agent to ask for ``high`` when small text or fine
#: detail decides the answer.
DEFAULT_DETAIL = "low"
_DETAIL_CHOICES = frozenset({"low", "high"})

#: Answer tokens one image gets. Enough for a paragraph of description or a
#: transcription; short enough that 200 of them do not become a document.
MAX_ANSWER_TOKENS = 1024

#: And the same bound on the way back, in characters. A model told to answer
#: briefly usually does; a model that decides to narrate would otherwise put
#: 200 essays into the context this tool exists to keep empty. Offloading
#: catches an oversized result only for an agent whose manifest sets
#: ``tool_offload_threshold``, which is off by default -- so the cap is here
#: too, where it does not depend on somebody's configuration.
MAX_ANSWER_CHARS = 2000
_TRUNCATION_MARK = " […truncated]"


@dataclass(frozen=True)
class Backend:
    """Which vision model answers, and how it is dialled.

    ``kind`` is ``"remote"`` (an OpenAI-compatible provider, images as data
    URIs) or ``"local"`` (this box's Ollama VLM). Two code paths, one because
    the sandbox has no Ollama and one because the box should not have to pay a
    provider to look at its own screenshots.
    """

    kind: str
    model: str


def _settings() -> Any:
    from robothor.settings import get_settings

    return get_settings()


def _configured_remote_model() -> str:
    """``ROBOTHOR_VISION_REMOTE_MODEL``, or empty when the box uses Ollama."""
    try:
        return str(_settings().providers.vision_remote_model or "").strip()
    except Exception:  # noqa: BLE001 - a missing config is "not configured", not a crash
        logger.debug("settings unavailable while resolving the remote vision model")
        return ""


def _configured_local_model() -> str:
    """``ROBOTHOR_VISION_MODEL`` — the same field ``view_image`` reads."""
    try:
        return str(_settings().ollama.vision_model or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("settings unavailable while resolving the local vision model")
        return ""


def _default_concurrency() -> int:
    try:
        return int(_settings().providers.vision_batch_concurrency)
    except Exception:  # noqa: BLE001
        return DEFAULT_CONCURRENCY


def _per_image_timeout() -> float:
    try:
        return float(_settings().providers.vision_batch_timeout)
    except Exception:  # noqa: BLE001
        return DEFAULT_TIMEOUT_SECONDS


def _batch_deadline() -> float:
    try:
        return float(_settings().providers.vision_batch_deadline)
    except Exception:  # noqa: BLE001
        return DEFAULT_DEADLINE_SECONDS


def resolve_backend() -> tuple[Backend | None, str]:
    """``(backend, refusal)`` — exactly one of the two is meaningful.

    A remote model the registry says cannot accept images is never dialled,
    however plainly it is configured: posting an image block to a text-only
    model is the failure ``view_image`` was fixed for in #578, and doing it
    two hundred times is that failure with a bill attached. When the operator
    has a local VLM as well, the batch falls back to it and says so; when they
    do not, this refuses rather than pretending.
    """
    remote = _configured_remote_model()
    local = _configured_local_model()
    if remote:
        from robothor.engine.model_registry import image_capability

        if image_capability(remote) != "rejects":
            return Backend("remote", remote), ""
        if not local:
            return None, (
                f"refused: the configured remote vision model ({remote}) does not accept "
                "images. Set ROBOTHOR_VISION_REMOTE_MODEL to a vision-capable model, or "
                "configure a local one with ROBOTHOR_VISION_MODEL."
            )
        logger.warning(
            "remote vision model %s does not accept images; falling back to the local VLM",
            remote,
        )
    if local:
        return Backend("local", local), ""
    return None, (
        "refused: no vision model is configured. Set ROBOTHOR_VISION_MODEL (local, via "
        "Ollama) or ROBOTHOR_VISION_REMOTE_MODEL (a vision-capable provider model)."
    )


def _workspace_root(workspace: str) -> Path | None:
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


def _resolve_one(raw: str, root: Path) -> tuple[Path | None, str]:
    """``(path, refusal)`` for one requested image. No backend call on refusal.

    Order matters and mirrors ``attachment_gate.resolve_for_send``: containment
    on the RESOLVED path (so a symlink out of the tree is caught), then the
    secret-path name rule BEFORE any stat (so a refusal does not reveal whether
    the file exists), then shape, then size.
    """
    text = str(raw or "").strip()
    if not text:
        return None, "path is required"

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if root != resolved and root not in resolved.parents:
        return None, (
            f"refused: {Path(text).name} resolves outside the workspace. Only images "
            "inside the workspace can be analyzed."
        )

    from robothor.engine.secret_paths import is_secret_path, refusal_for

    if is_secret_path(resolved):
        return None, refusal_for(resolved)

    if not resolved.is_file():
        return None, f"no such file: {text}"

    size = resolved.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return None, (
            f"refused: {resolved.name} is too large to analyze "
            f"({size // (1024 * 1024)} MB; the limit is {MAX_IMAGE_BYTES // (1024 * 1024)} MB)."
        )
    return resolved, ""


def _load(path: Path) -> tuple[bytes, str]:
    """``(bytes, mime)`` ready for a model. Blocking — always run in a thread.

    Pillow decoding is CPU-bound and synchronous. Four of these on the event
    loop would serialise the batch it exists to parallelise, and a large image
    would stall every other agent on the box while it decoded.
    """
    prepared = prepare_image_bytes(path)
    return prepared.data, prepared.mime


def _price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
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


async def _remote_answer(
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
            {"role": "system", "content": _SYSTEM_PROMPT},
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
        _price(backend.model, prompt_tokens, completion_tokens),
    )


#: What the vision model is told it is for. The literal, insistent form
#: ``view_image`` uses — a model that decides it "cannot access websites"
#: because the screenshot contains a URL has described nothing — with the
#: batch's own instruction to answer the question that was asked.
_SYSTEM_PROMPT = (
    "You are a vision system answering one question about one image. Answer the question "
    "directly and concretely, in at most a short paragraph. Read and transcribe any text "
    "that bears on the answer, exactly as shown. Never refuse, never say you cannot access "
    "websites or URLs — a URL in an image is text to be read, not a page to visit. If the "
    "image does not contain what was asked about, say so plainly rather than guessing."
)


async def _answer_one(
    backend: Backend, data: bytes, mime: str, question: str, detail: str, timeout: float
) -> tuple[str, int, float]:
    """``(answer, tokens, cost)`` from whichever backend is configured."""
    if backend.kind == "remote":
        answer, tokens, cost = await _remote_answer(backend, data, mime, question, detail, timeout)
        return _cap(answer), tokens, cost
    answer = (await describe_image_bytes(data, question, timeout=timeout)).strip()
    if not answer:
        raise RuntimeError("the vision model returned nothing")
    # The local VLM reports no usage and costs nothing: it is this box's GPU.
    return _cap(answer), 0, 0.0


def _cap(answer: str) -> str:
    """One answer, bounded. Marked when it was cut, never silently."""
    if len(answer) <= MAX_ANSWER_CHARS:
        return answer
    return answer[:MAX_ANSWER_CHARS].rstrip() + _TRUNCATION_MARK


async def _analyze_one(
    raw: str,
    *,
    backend: Backend,
    root: Path,
    question: str,
    detail: str,
    semaphore: asyncio.Semaphore,
    deadline: float,
) -> dict[str, Any]:
    """One image's row of the result. Never raises — a row always comes back."""
    started = time.monotonic()
    resolved, refusal = _resolve_one(raw, root)
    if resolved is None:
        return {"path": str(raw), "error": refusal, "ms": 0}

    async with semaphore:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "path": str(resolved),
                "error": (
                    "not analyzed — the batch ran out of time. Ask again with fewer "
                    "images, or a higher max_concurrency."
                ),
                "ms": int((time.monotonic() - started) * 1000),
            }
        timeout = min(_per_image_timeout(), remaining)
        try:
            data, mime = await asyncio.to_thread(_load, resolved)
        except UnsupportedImageError as exc:
            return {
                "path": str(resolved),
                "error": str(exc),
                "ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - Pillow raises widely on malformed input
            return {
                "path": str(resolved),
                "error": f"could not read as an image: {type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - started) * 1000),
            }

        try:
            answer, tokens, cost = await asyncio.wait_for(
                _answer_one(backend, data, mime, question, detail, timeout),
                timeout=timeout,
            )
        except TimeoutError:
            return {
                "path": str(resolved),
                "error": f"the vision model timed out after {timeout:.0f}s on this image",
                "ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - one image's failure, reported as one row
            logger.warning("vision call failed for %s: %s", resolved.name, exc)
            return {
                "path": str(resolved),
                "error": f"{type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - started) * 1000),
            }

    row: dict[str, Any] = {
        "path": str(resolved),
        "answer": answer,
        "model": backend.model,
        "ms": int((time.monotonic() - started) * 1000),
    }
    if tokens:
        row["tokens"] = tokens
    if cost:
        row["cost_usd"] = round(cost, 6)
    return row


def _clamp_concurrency(requested: Any) -> int:
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        wanted = _default_concurrency()
    if wanted <= 0:
        wanted = _default_concurrency()
    return max(1, min(wanted, MAX_CONCURRENCY))


async def analyze_images(
    *,
    paths: Any,
    question: str,
    detail: str = "",
    max_concurrency: Any = None,
    workspace: str = "",
) -> dict[str, Any]:
    """Answer *question* about every path in *paths*. Never returns a picture.

    The return shape is the contract the tool schema advertises: ``results`` in
    the order the paths were given, one row each, carrying either ``answer`` or
    ``error`` and never both.
    """
    if not isinstance(paths, (list, tuple)) or not paths:
        return {"error": f"paths is required: a list of 1–{MAX_PATHS} image paths"}
    if len(paths) > MAX_PATHS:
        return {
            "error": (
                f"refused: {len(paths)} images in one call, and the limit is {MAX_PATHS}. "
                "Split the work across calls."
            )
        }

    asked = question.strip()
    if not asked:
        return {"error": "question is required — say what you want to know about each image"}
    asked = asked[:MAX_QUESTION_CHARS]

    wanted_detail = (detail or "").strip().lower() or DEFAULT_DETAIL
    note = ""
    if wanted_detail not in _DETAIL_CHOICES:
        note = f"ignored detail={wanted_detail!r}; it must be one of low, high"
        wanted_detail = DEFAULT_DETAIL

    root = _workspace_root(workspace)
    if root is None:
        return {
            "error": (
                "refused: the workspace could not be resolved, so containment cannot be "
                "judged. Set ROBOTHOR_WORKSPACE."
            )
        }

    backend, refusal = resolve_backend()
    if backend is None:
        return {"error": refusal}

    concurrency = _clamp_concurrency(max_concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    deadline = time.monotonic() + _batch_deadline()
    started = time.monotonic()

    results = await asyncio.gather(
        *(
            _analyze_one(
                str(raw),
                backend=backend,
                root=root,
                question=asked,
                detail=wanted_detail,
                semaphore=semaphore,
                deadline=deadline,
            )
            for raw in paths
        )
    )

    answered = [r for r in results if "answer" in r]
    failed = len(results) - len(answered)
    elapsed = time.monotonic() - started
    tokens = sum(int(r.get("tokens") or 0) for r in results)
    cost = sum(float(r.get("cost_usd") or 0.0) for r in results)

    out: dict[str, Any] = {
        "question": asked,
        "model": backend.model,
        "backend": backend.kind,
        "analyzed": len(answered),
        "failed": failed,
        "results": list(results),
        "summary": (
            f"{len(answered)} of {len(results)} images answered by {backend.model} "
            f"in {elapsed:.1f}s ({concurrency} at a time)"
            + (f"; {failed} failed — see the per-image error" if failed else "")
        ),
    }
    if tokens:
        out["tokens"] = tokens
    if cost:
        # The runner adds a tool result's `cost_usd` to the run total, so an
        # out-of-band call that reported nothing would spend money invisibly.
        out["cost_usd"] = round(cost, 6)
    if note:
        out["note"] = note
    return out
