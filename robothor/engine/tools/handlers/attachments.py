"""``send_file`` — hand a file to the person this run is talking to.

The other half of the operator's 2026-09-15 complaint. There was no tool and no
delivery path that put a photo or a document in front of them: the only
``reply_document`` in the tree belonged to a slash command's markdown export.
An agent that produced a chart, a PDF, a screenshot or a CSV had to paste it as
text, describe it, or give up.

Where the refusals live
-----------------------
Not here. This tool takes a PATH and puts whatever is at it in front of a
person, which makes it the shortest exfiltration route the platform has — and
for a scheduled run it is **not the moment the bytes are sent**. The ladder
(containment, hard links, secret paths, the inbox secret flag, size, credential
shapes) is :func:`robothor.engine.attachment_gate.refuse_to_send`, and it runs
here AND again in ``delivery_attachments.send_attachments`` immediately before
the channel reads the file. See that module for why.

What stays here is everything specific to an agent asking: resolving the run's
own reply surface, the operator-tier ``target`` rule, the photo/document
rendering choice, the queue for a run nobody is watching, and the audit.

The send goes through ``Channel.send_attachment`` — the protocol slot, not a
Telegram call — and the result is whatever that channel could prove.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.engine.attachment_gate import digest_of, refuse_to_send, resolve_for_send

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Telegram's photo ceilings. Only the RENDERING choice lives here — whether an
#: image goes as a photo or as a document. Everything that decides whether a
#: file may leave the box at all is in ``robothor.engine.attachment_gate``,
#: because the tool is not the last place that question gets asked.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_DIMENSION_SUM = 10_000

#: Roles that may aim a file at a chat other than the one this run came from.
#: Everyone else sends to the conversation they are in. Redirecting a file is
#: how a prompt injection turns "show me the report" into "mail the report to
#: this other chat", and the run's own surface is the one address the platform
#: knows the operator chose.
_REDIRECT_ROLES = frozenset({"owner", "admin", "operator"})


def _workspace_root(ctx: Any) -> Path | None:
    """The tree a file must be inside to leave the box, or None if unresolvable."""
    configured = str(getattr(ctx, "workspace", "") or "")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    try:
        from robothor.settings.sources import workspace_path

        resolved = workspace_path()
        return resolved.resolve(strict=False) if resolved is not None else None
    except Exception:  # noqa: BLE001 - an unresolvable workspace refuses, never sends
        return None


async def _originating_route(ctx: Any) -> tuple[str, str]:
    """``(channel name, target)`` for the surface this run came from.

    Delegates to ``ask_user``'s resolution rather than re-deriving it: that
    module already knows how every trigger writes its own reply address, and a
    second parser would send files to a chat the ask path considers wrong.

    The run lookup is database-backed and synchronous, so it goes to a thread
    for the same reason ``ask_user`` sends it to one: a tool handler that
    blocked the engine loop on a query would stall every other chat, health
    check and agent session for its duration.
    """
    import asyncio

    from robothor.engine import tracking
    from robothor.engine.tools.handlers import ask_user

    run_id = str(getattr(ctx, "run_id", "") or "")
    if not run_id:
        return "", ""
    run = await asyncio.to_thread(tracking.get_run, run_id) or {}
    trigger = str(run.get("trigger_type") or "")
    detail = str(run.get("trigger_detail") or "")
    channel_name = ask_user._CHANNEL_FOR_TRIGGER.get(trigger, "")
    if channel_name == "telegram":
        return channel_name, ask_user._telegram_target(detail)
    if channel_name == "webchat":
        return channel_name, ask_user._webchat_target(ctx, detail)
    if trigger == ask_user.CHANNEL_DETAIL_PREFIX:
        return ask_user._plugin_channel_route(detail)
    return channel_name, ""


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """``(width, height)`` if this really decodes as an image, else None.

    Decoded, never inferred from the extension: a text file named ``.png`` must
    not be offered to Telegram as a photo, and a real PNG named ``.dat``
    should still go as one.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.size[0]), int(img.size[1])
    except Exception:  # noqa: BLE001 - "not an image" is the answer, not an error
        return None


async def send_file(args: dict[str, Any], ctx: ToolContext | Any = None) -> dict[str, Any]:
    """Send a file to the person this run is talking to.

    Takes ``(args, ctx)`` because that is how ``dispatch._execute_tool`` calls
    every handler.
    """
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return {"error": "path is required"}

    root = _workspace_root(ctx)
    if root is None:
        return {
            "error": (
                "refused: this run has no resolvable workspace, so there is no tree a file "
                "is allowed to be sent from."
            )
        }

    resolved, refusal = resolve_for_send(raw_path, root)
    if resolved is None:
        return {"error": refusal}

    # The whole ladder, from the shared gate. It runs again in
    # `delivery_attachments.send_attachments` immediately before the bytes are
    # read, because for a scheduled run this is not the moment of sending.
    refusal = refuse_to_send(resolved, root)
    if refusal:
        return {"error": refusal}

    size = resolved.stat().st_size

    channel_name, target = await _originating_route(ctx)
    requested_target = str(args.get("target") or "").strip()
    if requested_target and requested_target != target:
        role = str(getattr(ctx, "user_role", "") or "").lower()
        if role not in _REDIRECT_ROLES:
            return {
                "error": (
                    "refused: only an operator-tier agent may send a file to a chat other "
                    "than the one this run came from. Leave `target` out to reply here."
                )
            }
        target = requested_target

    if not channel_name or not target:
        # No live surface. A SCHEDULED run still has a delivery ahead of it, so
        # the file rides with the announcement rather than being refused —
        # pushing it now would put a PDF in the operator's chat minutes before
        # the report that explains it, or hours before nothing at all if the
        # run then failed.
        run_id = str(getattr(ctx, "run_id", "") or "")
        if run_id and not requested_target:
            from robothor.engine.delivery_attachments import (
                MAX_QUEUED_ATTACHMENTS,
                queue_attachment,
            )

            # Pinned to the bytes the ladder just approved and to the tree it
            # judged them in, so a file rewritten before the run ends is
            # refused at delivery rather than sent.
            if queue_attachment(
                run_id,
                str(resolved),
                str(args.get("caption") or ""),
                digest=digest_of(resolved),
                workspace=str(root),
            ):
                _audit(
                    ctx,
                    resolved,
                    size=size,
                    kind="queued",
                    target="(this run's delivery)",
                    delivered=False,
                )
                return {
                    "queued": True,
                    "file": resolved.name,
                    "size": size,
                    "note": (
                        f"Nobody is watching this run, so {resolved.name} will be sent with "
                        "its report when the run finishes. It arrives only if this agent "
                        "announces its output — an agent with `delivery.mode: none` reaches "
                        "nobody, and the file will not be sent."
                    ),
                }
            return {
                "error": (
                    f"refused: this run has already queued {MAX_QUEUED_ATTACHMENTS} files for "
                    "its report, which is the limit. Send fewer, larger ones."
                )
            }
        return {
            "error": (
                "send_file needs a person on the other end, and this run has no surface to "
                "reply on. Write the file where the operator can reach it and say where it is."
            )
        }

    as_, note = _rendering(args, resolved, size)

    from robothor.engine.channels import get_channel

    channel = get_channel(channel_name)
    if channel is None:
        return {"error": f"refused: the {channel_name} channel is not registered."}

    try:
        receipt = await channel.send_attachment(
            target, str(resolved), str(args.get("caption") or ""), as_=as_
        )
    except NotImplementedError as exc:
        # The honest refusal from a channel that cannot attach files. Passed
        # through verbatim: it already says what to do instead.
        return {"error": f"refused: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a third-party channel is third-party code
        logger.error("send_file failed on %s: %s", channel_name, exc)
        return {"error": f"the {channel_name} channel failed while sending: {exc}"}

    kind = "photo" if as_ == "photo" else "document"
    _audit(ctx, resolved, size=size, kind=kind, target=target, delivered=receipt.acknowledged > 0)

    if receipt.acknowledged <= 0:
        reason = receipt.status or "the channel acknowledged nothing"
        return {
            "sent": False,
            "error": (
                f"{resolved.name} was NOT delivered ({reason}). Tell the operator it did "
                "not arrive rather than assuming it did."
            ),
            "channel": channel_name,
            "target": target,
        }

    result: dict[str, Any] = {
        "sent": True,
        "as": kind,
        "file": resolved.name,
        "size": size,
        "channel": channel_name,
        "target": target,
        "message_id": receipt.platform_ids[0] if receipt.platform_ids else "",
    }
    if note:
        result["note"] = note
    return result


def _rendering(args: dict[str, Any], path: Path, size: int) -> tuple[str, str]:
    """``(as_, note)`` — photo or document, and why if it was not what was asked.

    ``auto`` sends an image as a photo when it fits Telegram's photo limits and
    as a document otherwise. Downgrading rather than refusing is deliberate: a
    document the operator can open beats an error, and a photo re-encoded down
    to fit would be a different picture from the one the agent made.
    """
    requested = str(args.get("as") or "auto").strip().lower()
    if requested == "document":
        return "document", ""

    dimensions = _image_dimensions(path)
    if dimensions is None:
        if requested == "photo":
            return "document", (
                f"{path.name} does not decode as an image, so it was sent as a document."
            )
        return "document", ""

    if size > MAX_PHOTO_BYTES:
        return "document", (
            f"{path.name} is too large to send as a photo, so it went as a document — "
            "open it to see the image."
        )
    if sum(dimensions) > MAX_PHOTO_DIMENSION_SUM:
        return "document", (
            f"{path.name} is {dimensions[0]}x{dimensions[1]}, too large to send as a photo, "
            "so it went as a document — open it to see the image."
        )
    return "photo", ""


def _audit(
    ctx: Any,
    path: Path,
    *,
    size: int,
    kind: str,
    target: str,
    delivered: bool,
) -> None:
    """Record that a file left the box. Never raises, never carries content.

    The BASENAME, not the path: a full path names the instance's own directory
    layout, and the audit row is read by people who need to know what was sent,
    not where it lived.
    """
    try:
        from robothor.audit.logger import log_event

        log_event(
            event_type="agent.file_sent",
            action="send_file",
            category="agent",
            actor=str(getattr(ctx, "agent_id", "") or "unknown"),
            user_id=str(getattr(ctx, "user_id", "") or ""),
            details={
                "file": path.name,
                "size": size,
                "kind": kind,
                "target": target,
                "tenant_id": str(getattr(ctx, "tenant_id", "") or ""),
                "run_id": str(getattr(ctx, "run_id", "") or ""),
            },
            status="ok" if delivered else "failed",
        )
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a send
        logger.debug("send_file audit failed", exc_info=True)


HANDLERS["send_file"] = send_file
