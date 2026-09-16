"""Let an agent look at a picture.

The engine could already deliver an image to a model — `session.py` builds a
proper `image_url` content block — but only for two hardcoded tool names,
`desktop_screenshot` and `browser`. Nothing could show the agent a file. The
one other image path in the tree lives inside the PDF handler, which
side-calls a hardcoded Gemini and returns the agent text about the picture
rather than the picture.

Measured cost, 2026-08-25: four WildClawBench Code Intelligence tasks hand
the agent a PNG and ask it to read a grid off it. Genus scored 0 on all four
while the competing harness — same model, GLM 5.2, which is multimodal —
scored 93/88/30/22. Our agent had been writing pixel-inspection code with
PIL and reasoning about arrays through text, because that was the only way
it could "see".

The output convention (`image_base64` + `image_mime`) is what `session.py`
now keys on, so any future tool that produces an image gets the same
treatment without touching the session.

Telling the truth about who looked
----------------------------------
Returning those blocks unconditionally was its own defect, and the operator
found it on 2026-09-15. A text-only model answers an image block with a hard
refusal; `llm_client._call_with_image_fallback` catches that, strips the
blocks and retries — so the agent was handed a caption where the picture had
been and went on believing it had looked. Every result now carries `seen_by`:

* ``primary``      — the blocks are in the result; the agent's own model sees them.
* ``vision-model`` — the model cannot accept images, so the local VLM's
  description is in the result INSTEAD of blocks the client would only strip.
* ``nobody``       — neither worked. Said plainly, with an ``error``, because a
  description nobody produced is the one thing this must never invent.

The capability comes from `model_registry.image_capability`, which answers
``unknown`` for a model nobody has declared. ``unknown`` keeps the blocks (a
multimodal model missing from the curated table must still be shown pictures)
and says so in ``note``.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Longest edge, in pixels, that reaches the model. Provider payload limits
#: are real (a 6000px screenshot base64s past several megabytes), but the
#: answer is to downscale rather than refuse: a shrunk image is still a
#: usable one, and an error is a lost capability.
MAX_DIMENSION = 1568

#: Formats worth sending. Checked by DECODING the file, never by extension —
#: a text file named `.png` must not reach a model as an image, and a real
#: PNG named `.dat` should still work.
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/png",
    "TIFF": "image/png",
}

#: Formats a provider will not accept: re-encoded to PNG on the way out.
_REENCODE_TO_PNG = {"BMP", "TIFF", "GIF"}


#: Extensions worth offering when the requested one does not exist. Derived
#: from the formats this tool can actually decode, so the two cannot drift.
_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
)


def _same_stem_images(path: Path) -> list[Path]:
    """Images beside `path` sharing its stem, for an extension that missed.

    An agent asking for `figure.png` when the file is `figure.jpg` used to
    get "no such file" and had to recover by guessing. Eleven of the twelve
    Code Intelligence tasks are image tasks, so that recovery was being paid
    for in the category where this harness trails furthest.

    Only real files count — a directory named `assets.jpg` is not an image —
    and the caller substitutes only when exactly one candidate exists. Two
    candidates is a question for the agent, not a coin flip for the tool.
    """
    parent = path.parent
    if not parent.is_dir():
        return []
    stem = path.stem
    # iterdir, not glob: a stem containing `[` or `*` is a valid filename and
    # a glob pattern, and the two disagree.
    return sorted(
        candidate
        for candidate in parent.iterdir()
        if candidate.stem == stem
        and candidate.is_file()
        and candidate.suffix.lower() in _IMAGE_SUFFIXES
    )


#: What the local VLM is asked when nobody said what to look for. Literal and
#: insistent: a vision model that decides it "cannot access websites" because
#: the screenshot contains a URL has described nothing.
DEFAULT_VISION_PROMPT = (
    "Describe what you see in this image in detail. Read and transcribe ALL visible text "
    "exactly. Note any URLs, names, numbers, UI elements, or content shown."
)

#: The system prompt for the same call. Moved here from
#: ``engine/telegram_handlers._analyze_photo_bytes`` so one description path
#: serves the inbound channel and the tool, rather than the channel owning a
#: private copy the tool could not reach.
_VISION_SYSTEM = (
    "You are the local vision system. Your ONLY job is to describe what is visible in "
    "images. You MUST always describe image content — never refuse, never say you cannot "
    "access websites or URLs. If you see a URL in an image, READ and REPORT the text — do "
    "not try to visit it. Transcribe ALL visible text exactly as shown. Report people, UI "
    "elements, screenshots, diagrams, app names, domain names, error messages, and any "
    "other visible content. Be literal and precise."
)

#: Seconds the local VLM gets. Generous because a 27B-class model on a busy GPU
#: is slow, bounded because an inbound photo must not hold a chat open forever.
VISION_TIMEOUT_SECONDS = 120.0


def vision_model_name() -> str:
    """Which local model describes pictures, from settings.

    Read through ``get_settings()`` rather than ``os.environ``: the handler this
    moved from read a raw ``OLLAMA_URL`` and a hardcoded model id, so the
    instance's declared vision model was ignored by the one path that used it
    most.
    """
    try:
        from robothor.settings import get_settings

        return str(get_settings().ollama.vision_model or "")
    except Exception:  # noqa: BLE001 - a description must not depend on config loading
        return ""


async def describe_image_bytes(
    data: bytes,
    prompt: str = "",
    *,
    timeout: float = VISION_TIMEOUT_SECONDS,
) -> str:
    """The local vision model's description of *data*, or raise.

    Raises rather than returning an error string: a caller that cannot tell a
    description from a failure will eventually show the failure to the operator
    as though it were what the picture contains. ``view_image`` catches this and
    answers ``seen_by: "nobody"``.
    """
    import httpx

    from robothor.settings import get_settings

    settings = get_settings()
    model = str(settings.ollama.vision_model or "")
    if not model:
        raise RuntimeError("no local vision model is configured (ROBOTHOR_VISION_MODEL)")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM},
            {
                "role": "user",
                "content": prompt or DEFAULT_VISION_PROMPT,
                "images": [base64.b64encode(data).decode("ascii")],
            },
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1024, "num_gpu": 999},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{settings.ollama.base_url}/api/chat", json=payload)
        response.raise_for_status()
        content = str((response.json().get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("the local vision model returned nothing")
    return content


def _capability_for_caller() -> tuple[str, str]:
    """``(model id, "accepts"|"rejects"|"unknown")`` for whoever is asking.

    The model is the one the current task is dialling, recorded by
    ``llm_client`` at the moment of the call. Nothing is inferred from the
    manifest here on purpose: reading the fleet chain would make a tool result
    depend on files outside the run, and a test or a benchmark would answer
    differently from production for reasons nobody could see.
    """
    from robothor.engine.model_registry import active_model, image_capability

    model = active_model()
    return model, image_capability(model)


class UnsupportedImageError(ValueError):
    """This file cannot be turned into something a model may be shown.

    Carries the exact sentence the tool returns, so the two callers of
    :func:`prepare_image_bytes` cannot drift into two different refusals for
    the same file.
    """


@dataclass(frozen=True)
class PreparedImage:
    """The bytes a model gets, plus what was done to them on the way.

    Extracted from ``view_image`` when ``analyze_image`` arrived: the size and
    format rules are the ones a provider actually enforces, and a second
    implementation of them is a second set of files that silently fail to
    reach a model.
    """

    data: bytes
    mime: str
    width: int
    height: int
    original_width: int
    original_height: int

    @property
    def downscaled(self) -> bool:
        return (self.width, self.height) != (self.original_width, self.original_height)


def prepare_image_bytes(path: Path) -> PreparedImage:
    """*path* decoded, size-limited and re-encoded if the format needs it.

    Raises :class:`UnsupportedImageError` for a file no model will accept, and lets
    Pillow's own exceptions out for a file that is not an image at all — the
    two callers report them differently and neither should have to guess which
    happened.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow ships with the image extra
        raise UnsupportedImageError("Pillow is not installed — cannot read images") from exc

    with Image.open(path) as img:
        img.load()
        fmt = (img.format or "").upper()
        mime = _MIME_BY_FORMAT.get(fmt)
        if mime is None:
            raise UnsupportedImageError(f"unsupported image format: {fmt or 'unknown'}")

        original = img.size
        # Typed as the base Image, not ImageFile: `resize` returns the
        # former, and LANCZOS moved to the Resampling enum in Pillow 10.
        work: Image.Image = img
        if max(original) > MAX_DIMENSION:
            scale = MAX_DIMENSION / max(original)
            work = img.resize(
                (max(1, int(original[0] * scale)), max(1, int(original[1] * scale))),
                Image.Resampling.LANCZOS,
            )

        if fmt in _REENCODE_TO_PNG or work is not img:
            buf = io.BytesIO()
            # Alpha and palette modes do not survive every encoder; RGB is
            # the safe common denominator for anything being re-encoded.
            work.convert("RGB").save(buf, format="PNG")
            data = buf.getvalue()
            mime = "image/png"
        else:
            data = path.read_bytes()

        return PreparedImage(
            data=data,
            mime=mime,
            width=work.size[0],
            height=work.size[1],
            original_width=original[0],
            original_height=original[1],
        )


async def view_image(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Return an image file as content the agent's own model can see.

    Takes `(args, ctx)` because that is how `dispatch._execute_tool` calls
    every handler. A one-argument version passed its unit tests and then
    raised `TypeError` on the first real call — the tests were calling it
    directly, in a shape production never uses.
    """
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return {"error": "path is required"}

    path = Path(raw_path).expanduser()
    resolved_from: str | None = None
    if not path.is_file():
        siblings = _same_stem_images(path)
        if len(siblings) == 1:
            resolved_from = str(siblings[0])
            path = siblings[0]
        elif siblings:
            names = ", ".join(sorted(s.name for s in siblings))
            return {
                "error": (
                    f"no such file: {raw_path} — several images share that name "
                    f"({names}). Ask for the one you want."
                )
            }
        else:
            return {"error": f"no such file: {raw_path}"}

    try:
        prepared = prepare_image_bytes(path)
    except UnsupportedImageError as e:
        return {"error": str(e)}
    except Exception as e:  # Pillow raises a wide family on malformed input
        return {"error": f"could not read as an image: {type(e).__name__}: {e}"}

    data = prepared.data
    model, capability = _capability_for_caller()
    result: dict[str, Any] = {
        "width": prepared.width,
        "height": prepared.height,
        "path": str(path),
        "resolved_from": resolved_from,
    }
    if capability == "rejects":
        # No blocks. The client would strip them and the agent would be
        # told it looked at something it never saw.
        result["model"] = model
        try:
            result["description"] = await describe_image_bytes(data, str(args.get("prompt") or ""))
            result["seen_by"] = "vision-model"
            result["note"] = (
                f"{model or 'this model'} cannot accept images, so this is the local "
                "vision model's description rather than the picture itself. Treat it "
                "as a second-hand account: if a detail decides the task, read the "
                "file programmatically to confirm it."
            )
        except Exception as exc:  # noqa: BLE001 - reported, never invented
            logger.warning("local vision model could not describe %s: %s", path.name, exc)
            result["seen_by"] = "nobody"
            result["error"] = (
                f"{model or 'this model'} cannot accept images and the local vision "
                f"model is unavailable ({type(exc).__name__}). Nobody has looked at "
                f"{path.name}. Inspect it programmatically — e.g. Pillow via exec — "
                "or say plainly that you could not see it."
            )
        return result

    result["image_base64"] = base64.b64encode(data).decode("ascii")
    result["image_mime"] = prepared.mime
    result["seen_by"] = "primary"
    # Notes ACCUMULATE. Each of these used to assign `note` outright,
    # so a substituted file that was also downscaled reported only the
    # downscale and the agent never learned it had been handed a
    # different file from the one it asked for.
    notes: list[str] = []
    if capability == "unknown":
        notes.append(
            f"{model or 'the current model'} is not confirmed to accept images. If "
            "the picture does not appear, call view_image again — the refusal is "
            "recorded and you will be given the local description instead."
        )
    if resolved_from:
        notes.append(
            f"{raw_path} does not exist; read {Path(resolved_from).name} "
            "instead, which shares its name"
        )
    if prepared.downscaled:
        result["original_width"] = prepared.original_width
        result["original_height"] = prepared.original_height
        notes.append(
            f"downscaled from {prepared.original_width}x{prepared.original_height} to fit "
            f"the {MAX_DIMENSION}px limit — fine detail may be lost"
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


async def analyze_image(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Answer one question about many images, OUT of the agent's own context.

    The counterpart to ``view_image``, and the reason both exist: this one
    never returns a picture. Each image is sent to the vision backend on its
    own, concurrently, and only the text answer comes back — so an agent can
    afford to ask about a hundred files, which is the difference between 0.28
    and 0.99 on the benchmark task that measured it. See
    :mod:`robothor.engine.vision_batch`.
    """
    from robothor.engine.vision_batch import analyze_images

    return await analyze_images(
        paths=args.get("paths"),
        question=str(args.get("question") or ""),
        detail=str(args.get("detail") or ""),
        max_concurrency=args.get("max_concurrency"),
        workspace=str(getattr(ctx, "workspace", "") or ""),
        run_id=str(getattr(ctx, "run_id", "") or ""),
    )


HANDLERS: dict[str, Any] = {"view_image": view_image, "analyze_image": analyze_image}
