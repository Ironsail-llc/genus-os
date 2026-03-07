"""PDF analysis tool handler."""

from __future__ import annotations

from typing import Any

from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _parse_page_range(spec: str | None, total: int, max_pages: int = 10) -> list[int]:
    """Parse a page range specification into a list of 0-based indices."""
    if not spec:
        return list(range(min(total, max_pages)))

    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start_i = max(0, int(start) - 1)
            end_i = min(total, int(end))
            indices.update(range(start_i, end_i))
        else:
            i = int(part) - 1
            if 0 <= i < total:
                indices.add(i)

    return sorted(indices)[:max_pages]


async def _handle_analyze_pdf(
    args: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    """Analyze a PDF: fast-path text extraction, slow-path vision AI."""
    from pathlib import Path

    path_str = args.get("path", "")
    query = args.get("query")
    pages_spec = args.get("pages")
    workspace = ctx.workspace

    if not path_str:
        return {"error": "No path provided"}

    max_pdf_size = 50 * 1024 * 1024  # 50MB

    # Resolve path (workspace-relative or absolute)
    path = Path(path_str)
    if not path.is_absolute() and workspace:
        path = Path(workspace) / path

    # Path traversal protection
    resolved = path.resolve()
    if workspace:
        workspace_resolved = Path(workspace).resolve()
        if not resolved.is_relative_to(workspace_resolved):
            return {"error": "Path must be within workspace"}

    if not resolved.exists():
        return {"error": f"File not found: {path}"}
    if not str(resolved).lower().endswith(".pdf"):
        return {"error": "File is not a PDF"}
    if resolved.stat().st_size > max_pdf_size:
        return {"error": f"PDF exceeds {max_pdf_size // (1024 * 1024)}MB limit"}

    try:
        import io

        import pypdf

        raw_bytes = resolved.read_bytes()
        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        total_pages = len(reader.pages)

        # Parse page range
        page_indices = _parse_page_range(pages_spec, total_pages, max_pages=10)

        # Fast path: text extraction
        text_pages = []
        for i in page_indices:
            text = reader.pages[i].extract_text() or ""
            if text.strip():
                text_pages.append(f"[Page {i + 1}]\n{text}")

        if text_pages and not query:
            return {
                "pages_analyzed": len(page_indices),
                "page_count": total_pages,
                "text_content": "\n\n".join(text_pages)[:8000],
            }

        if text_pages and query:
            text_content = "\n\n".join(text_pages)[:6000]
            try:
                import litellm

                response = await litellm.acompletion(
                    model="gemini/gemini-2.5-flash",
                    messages=[
                        {
                            "role": "system",
                            "content": "Answer the question based on the PDF content provided.",
                        },
                        {
                            "role": "user",
                            "content": f"PDF Content:\n{text_content}\n\nQuestion: {query}",
                        },
                    ],
                    temperature=0.1,
                    max_tokens=2000,
                )
                ai_answer = response.choices[0].message.content
                return {
                    "pages_analyzed": len(page_indices),
                    "page_count": total_pages,
                    "text_content": text_content[:4000],
                    "ai_analysis": ai_answer,
                }
            except Exception as e:
                return {
                    "pages_analyzed": len(page_indices),
                    "page_count": total_pages,
                    "text_content": text_content[:8000],
                    "ai_analysis_error": str(e),
                }

        # Slow path: image-based PDF
        try:
            import base64

            images_b64 = []
            for i in page_indices[:5]:
                page = reader.pages[i]
                if "/XObject" in (page.get("/Resources") or {}):
                    xobjects = page["/Resources"]["/XObject"].get_object()
                    for obj_name in xobjects:
                        xobj = xobjects[obj_name].get_object()
                        if xobj["/Subtype"] == "/Image":
                            data = xobj.get_data()
                            if len(data) > 100:
                                images_b64.append(base64.b64encode(data).decode())
                                break

            if not images_b64:
                return {
                    "pages_analyzed": len(page_indices),
                    "page_count": total_pages,
                    "text_content": "[No extractable text or images found in this PDF]",
                }

            import litellm

            content: list[dict] = []
            for idx, img_b64 in enumerate(images_b64[:3]):
                content.append({"type": "text", "text": f"Page {page_indices[idx] + 1}:"})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    }
                )

            prompt = query or "Extract and describe all text and content from these PDF pages."
            content.append({"type": "text", "text": prompt})

            response = await litellm.acompletion(
                model="gemini/gemini-2.5-flash",
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=4000,
            )
            return {
                "pages_analyzed": len(page_indices),
                "page_count": total_pages,
                "ai_analysis": response.choices[0].message.content,
            }

        except ImportError:
            return {
                "pages_analyzed": 0,
                "page_count": total_pages,
                "text_content": "[Image-based PDF — install Pillow for vision analysis]",
            }
        except Exception as e:
            return {
                "pages_analyzed": 0,
                "page_count": total_pages,
                "error": f"Vision analysis failed: {e}",
            }

    except ImportError:
        return {"error": "pypdf not installed — install with: pip install pypdf"}
    except Exception as e:
        return {"error": f"PDF analysis failed: {e}"}


HANDLERS["analyze_pdf"] = _handle_analyze_pdf
