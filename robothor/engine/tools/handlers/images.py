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
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

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
    if not path.is_file():
        return {"error": f"no such file: {raw_path}"}

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the image extra
        return {"error": "Pillow is not installed — cannot read images"}

    try:
        with Image.open(path) as img:
            img.load()
            fmt = (img.format or "").upper()
            mime = _MIME_BY_FORMAT.get(fmt)
            if mime is None:
                return {"error": f"unsupported image format: {fmt or 'unknown'}"}

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

            reencode = fmt in _REENCODE_TO_PNG or work is not img
            if reencode:
                buf = io.BytesIO()
                # Alpha and palette modes do not survive every encoder; RGB is
                # the safe common denominator for anything being re-encoded.
                work.convert("RGB").save(buf, format="PNG")
                data = buf.getvalue()
                mime = "image/png"
            else:
                data = path.read_bytes()

            result: dict[str, Any] = {
                "image_base64": base64.b64encode(data).decode("ascii"),
                "image_mime": mime,
                "width": work.size[0],
                "height": work.size[1],
                "path": str(path),
            }
            if work.size != original:
                result["original_width"] = original[0]
                result["original_height"] = original[1]
                result["note"] = (
                    f"downscaled from {original[0]}x{original[1]} to fit the "
                    f"{MAX_DIMENSION}px limit — fine detail may be lost"
                )
            return result
    except Exception as e:  # Pillow raises a wide family on malformed input
        return {"error": f"could not read as an image: {type(e).__name__}: {e}"}


HANDLERS: dict[str, Any] = {"view_image": view_image}
