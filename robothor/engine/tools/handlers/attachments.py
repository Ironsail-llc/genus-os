"""``send_file`` — hand a file to the person this run is talking to.

The other half of the operator's 2026-09-15 complaint. There was no tool and no
delivery path that put a photo or a document in front of them: the only
``reply_document`` in the tree belonged to a slash command's markdown export.
An agent that produced a chart, a PDF, a screenshot or a CSV had to paste it as
text, describe it, or give up.

Why the refusals are most of this file
--------------------------------------
This tool takes a PATH and puts whatever is at it in front of a person, which
makes it the shortest exfiltration route the platform has. A model that has been
talked into reading a credential cannot leak it through ``read_file`` (the
redactor catches the value) but could hand over the whole file here. So, in
order, and all before a byte is uploaded:

1. **Containment.** The RESOLVED path — symlinks followed — must be inside the
   workspace. A symlink in the workspace pointing at ``/etc/robothor`` is the
   probe this ordering exists for.
2. **Secret paths.** :mod:`robothor.engine.secret_paths`, the same rule
   ``read_file`` and ``exec`` enforce, so a file is not sendable merely because
   it is unreadable.
3. **Credential shapes.** For files that are actually text, the export gate's
   own scanner (:func:`robothor.templates.bundle.scan_secret_literals`). One
   opinion about what a credential looks like, asked twice, rather than a
   second list here that drifts from it. The refusal names the file and the
   family, never the value.
4. **Size.** Checked against the platform's ceiling before the read.

Then the send goes through ``Channel.send_attachment`` — the protocol slot, not
a Telegram call — and the result is whatever that channel could prove.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Telegram's ceilings, mirrored here so the refusal happens before the upload
#: rather than as a provider error the agent has to interpret. Kept in this
#: module as well as in ``channels/telegram`` because the tool refuses and the
#: channel enforces, and a tool that trusted the channel to refuse would have
#: read a 2 GB file into memory first.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_DIMENSION_SUM = 10_000
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

#: Roles that may aim a file at a chat other than the one this run came from.
#: Everyone else sends to the conversation they are in. Redirecting a file is
#: how a prompt injection turns "show me the report" into "mail the report to
#: this other chat", and the run's own surface is the one address the platform
#: knows the operator chose.
_REDIRECT_ROLES = frozenset({"owner", "admin", "operator"})

#: Suffixes worth scanning as text for credential shapes. A PNG decoded as
#: UTF-8 is noise, and grepping noise refuses real files at random; the scan is
#: for text an agent could have written a secret INTO.
_SCANNABLE = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".py",
        ".js",
        ".ts",
        ".sh",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".log",
        ".sql",
        ".rst",
        ".tex",
        ".env",
        "",
    }
)

#: Largest text file that is scanned in full. Above this the file is refused
#: rather than sent unscanned: "too big to check" must not mean "sent anyway".
MAX_SCAN_BYTES = 4 * 1024 * 1024


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


def _saved_as_a_secret(path: Path) -> bool:
    """Is this an inbox copy of a file that was named like a credentials file?

    The inbox stores ``<file_unique_id>-<sanitised name>``, and sanitising
    destroys what ``secret_paths`` matches on: ``.env`` becomes ``env`` and
    ``id_rsa`` keeps only its stem. So the stored name is stripped of its uid
    prefix and asked again, with the leading dot restored — otherwise a file the
    operator sent could be sent back out under a name the rules no longer
    recognise, having been refused on the way in.
    """
    from robothor.engine.attachments import INBOX_DIRNAME, holds_credentials

    if INBOX_DIRNAME not in path.parts:
        return False
    stored = path.name.split("-", 1)[-1] if "-" in path.name else path.name
    return holds_credentials(stored) or holds_credentials(f".{stored}")


def _credential_refusal(path: Path, size: int) -> str | None:
    """Why this file may not leave the box, or None.

    Only text is scanned, and only up to :data:`MAX_SCAN_BYTES`; a text file
    too large to check is refused rather than sent unchecked.
    """
    if _saved_as_a_secret(path):
        return (
            f"refused: {path.name} was received over a channel and is named like a "
            "credentials file. It is kept on disk, but its contents do not leave the box."
        )
    if path.suffix.lower() not in _SCANNABLE:
        return None
    if size > MAX_SCAN_BYTES:
        return (
            f"refused: {path.name} is a {size // 1024 // 1024} MB text file, too large to check "
            "for credentials before sending. Send a smaller extract of it."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Not decodable as text, so the text scan does not apply to it.
        return None

    from robothor.templates.bundle import scan_secret_literals

    findings = scan_secret_literals(text, path.name)
    if not findings:
        return None
    # The reason, never the value: an error that helpfully echoes the credential
    # publishes it to the transcript, which is the exposure being refused.
    reasons = sorted({finding.reason for finding in findings})
    lines = sorted({finding.line for finding in findings})[:5]
    return (
        f"refused: {path.name} contains {reasons[0]} (line "
        f"{', '.join(str(line) for line in lines)}). Credentials must not leave the box in a "
        "file. Remove the value or send a redacted copy."
    )


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

    # Relative paths resolve against the workspace, which is also what makes
    # `../../etc/passwd` land outside it and be refused below.
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    # strict=False so a missing file is reported as missing rather than as an
    # OSError, and symlinks are followed BEFORE containment is judged.
    resolved = candidate.resolve(strict=False)

    if root != resolved and root not in resolved.parents:
        return {
            "error": (
                f"refused: {Path(raw_path).name} resolves outside the workspace. Only files "
                "inside the workspace (including the channel inbox) can be sent."
            )
        }

    from robothor.engine.secret_paths import is_secret_path, refusal_for

    if is_secret_path(resolved):
        return {"error": refusal_for(resolved)}

    if not resolved.exists():
        return {"error": f"no such file: {raw_path}"}
    if not resolved.is_file():
        return {"error": f"refused: {resolved.name} is not a file. Send one file at a time."}

    size = resolved.stat().st_size
    if size == 0:
        return {"error": f"refused: {resolved.name} is empty — there is nothing to send."}
    if size > MAX_DOCUMENT_BYTES:
        from robothor.engine.attachments import human_size

        return {
            "error": (
                f"refused: {resolved.name} is {human_size(size)}, over the "
                f"{human_size(MAX_DOCUMENT_BYTES)} a chat attachment may be. Put it somewhere "
                "the operator can fetch it and send the link instead."
            )
        }

    refusal = _credential_refusal(resolved, size)
    if refusal:
        return {"error": refusal}

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

            if queue_attachment(run_id, str(resolved), str(args.get("caption") or "")):
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
