"""What can deliver, whether it works — the engine half of the Channels page.

``genus channel list`` and ``genus channel verify`` are the only way to answer
two questions today, and both of them need a shell on the box: *what could a
manifest's ``delivery.channel`` resolve to*, and *does that channel actually
reach anybody*. Between them sits every setup failure a channel has — a bot
token pasted from the app-token field, an app installed without ``chat:write``,
a workspace the bot was never invited to — none of which is visible until a
briefing does not turn up.

The work lives here rather than in the bridge for the reason
``admin_providers`` gives: a channel is an *object in this process*, holding
this process's credentials, and the bridge cannot ask one anything. So the
engine answers and the bridge proxies.

Three rules this module is built around:

* **A health report is a diagnostic, never a credential.** ``health()`` is
  implemented per channel — including by plugins this platform has never seen
  — so the redaction happens HERE, on the way out, rather than resting on every
  implementor having been careful. A secret-shaped value is replaced by a
  fingerprint: enough to tell two credentials apart, and nothing of either.
* **One slow channel is not a broken page.** ``SlackChannel.health`` makes an
  ``auth.test`` round trip, so every report is time-boxed and a timeout is a
  reported field rather than a 500 for the whole listing.
* **"Not configured" and "failed" are different answers.** ``verify`` signals
  the first with a single ``UNCONFIGURED_STEP``; that is what
  ``genus channel verify``'s exit code 2 means, and it is preserved here rather
  than flattened into a red cross. A channel with no ``verify`` gets an empty
  step list — never a fabricated pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from robothor.engine.channels.base import UNCONFIGURED_STEP
from robothor.secrets.redaction import redact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = ["channel_status", "register", "verify_channel"]

#: How long one channel's ``health()`` may take before the listing gives up on
#: it. An operator is watching a page; a channel that cannot answer in five
#: seconds is itself the finding.
HEALTH_TIMEOUT_SECONDS = 5.0

#: How long a ``verify()`` may take. Longer than a health report because it is
#: aimed by hand and does real work — an auth test, a post, an SMTP session —
#: and shorter than the bridge's own proxy timeout, so a hang is reported as a
#: failed step by the process that knows which step it hung on rather than as a
#: 502 by the one that does not.
VERIFY_TIMEOUT_SECONDS = 20.0

#: The longest ``target`` this will hand a channel. An email address caps at
#: 320 octets (RFC 5321) and a Slack channel id is far shorter; beyond that the
#: value is not a target, and it reaches a third-party client either way.
MAX_TARGET_CHARS = 320

#: Field names whose VALUE is a credential regardless of what it looks like.
#: Substring matched, lower-cased: ``bot_token``, ``app_token``, ``smtp_password``
#: and a plugin's ``apiKey`` all land here.
#:
#: ``chat_id`` is on the list and is not a credential: it is the address of the
#: operator's own private conversation, and possession of it plus a token is
#: the whole of what sending as this instance requires. The Channels page needs
#: to know a chat id is CONFIGURED, never what it is.
_SECRET_NAME_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "chat_id",
    "chatid",
    "authorization",
    "cookie",
    "signing",
    "webhook",
)

#: How deep the redactor walks a report. A health dict is a flat-ish diagnostic;
#: this exists so a plugin returning a self-referential structure cannot turn a
#: status page into a stack overflow.
_MAX_DEPTH = 6


class VerifyRequest(BaseModel):
    """What to aim a verify at. ``None`` means the channel's own default."""

    target: str | None = Field(default=None, max_length=MAX_TARGET_CHARS)


def _fingerprint(value: object) -> str:
    """Enough to tell two credentials apart, and nothing of either.

    A prefix of the value itself would be worse than useless — Slack tokens
    share their first two segments — so this is the same SHA-256 prefix
    ``genus channel add`` prints, in the same ``sha256:`` form the provider
    surface uses.
    """
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:12]
    return f"sha256:{digest}"


def _redact(value: Any, *, secret_name: bool = False, depth: int = 0) -> Any:
    """``value`` with anything credential-shaped replaced.

    Two independent tests, because each catches what the other misses. The
    NAME catches a credential that has no shape — an SMTP password is whatever
    the mail provider issued — and the SHAPE catches one reported under an
    innocent name, which is how a token reached a log line on this instance
    before (``robothor/secrets/redaction.py`` carries that history).
    """
    if depth > _MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): _redact(
                item,
                secret_name=_is_secret_name(str(key)),
                depth=depth + 1,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secret_name=secret_name, depth=depth + 1) for item in value]
    if isinstance(value, bool) or value is None:
        # A bool under a secret name is "is it set", which is exactly the answer
        # the page wants — and a fingerprint of ``"True"`` would be the same
        # twelve characters on every instance in the world.
        return value
    if secret_name:
        # BEFORE the scalar short-circuit, not after it. A Telegram chat id is
        # an INTEGER, and ``chat_id`` is the field this redactor's docstring
        # singles out; short-circuiting on type first meant the one shape the
        # named field actually takes went through untouched.
        return _fingerprint(value)
    if isinstance(value, (int, float)):
        return value
    return redact(str(value))


def _is_secret_name(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_NAME_HINTS)


def _configured(report: dict[str, Any]) -> bool | None:
    """Whether the channel says it is set up, per its own health report.

    Three field names rather than one because three are in use: ``email`` and
    ``slack`` say ``configured``, ``event_bus`` says ``enabled`` and
    ``telegram`` says ``sender_registered``. A listing that understood only the
    first would report the two channels most instances actually have as unknown.

    ``None`` — unknown — for a report that answers none of them, which is
    failing closed on the optimistic side: a plugin whose health says nothing
    about configuration must not be painted green.
    """
    for key in ("configured", "enabled", "sender_registered"):
        if key in report:
            return bool(report[key])
    return None


async def _health_report(name: str, channel: Any) -> dict[str, Any]:
    """One channel's health, time-boxed, redacted, and never raising.

    Every failure mode is a FIELD: a timeout, an exception, a channel that
    returns something that is not a mapping. The alternative is a Settings page
    that shows nothing at all because one plugin is wedged.
    """
    try:
        raw = await asyncio.wait_for(channel.health(), timeout=HEALTH_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "channel %s did not report health within %.1fs", name, HEALTH_TIMEOUT_SECONDS
        )
        return {
            "channel": name,
            "timed_out": True,
            "error": f"health() did not answer within {HEALTH_TIMEOUT_SECONDS:.0f}s",
        }
    except Exception as exc:  # noqa: BLE001 — one broken channel is not a broken page
        return {"channel": name, "error": redact(f"{type(exc).__name__}: {exc}")}
    if not isinstance(raw, dict):
        return {"channel": name, "error": "health() did not return a report"}
    report: dict[str, Any] = _redact(raw)
    report.setdefault("channel", name)
    return report


def _can_verify(channel: Any) -> bool:
    return callable(getattr(channel, "verify", None))


async def channel_status() -> dict[str, Any]:
    """Every channel a delivery could currently resolve to, and its state.

    The channels are asked in PARALLEL: they are independent, several of them
    make a network round trip, and the serial version cost the sum of every
    timeout on an instance where two providers were down.
    """
    from robothor.engine.channels import registry

    channels = registry.list_channels()
    names = sorted(channels)
    reports = await asyncio.gather(*(_health_report(name, channels[name]) for name in names))
    return {
        "channels": [
            {
                "name": name,
                "builtin": name in registry.BUILTIN_CHANNELS,
                "configured": _configured(report),
                "health": report,
                "verify_available": _can_verify(channels[name]),
            }
            for name, report in zip(names, reports, strict=True)
        ]
    }


def _checked_target(target: str | None) -> str | None:
    """A target, or a refusal. Never a control character.

    The value reaches a third-party client — ``chat.postMessage``, an SMTP
    envelope — and a newline in it is the header-splitting shape those clients
    have historically been vulnerable to. A channel's own validation runs
    afterwards; this one is about what may leave the process at all.
    """
    from fastapi import HTTPException

    if target is None:
        return None
    clean = target.strip()
    if not clean:
        return None
    if len(clean) > MAX_TARGET_CHARS or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in clean):
        raise HTTPException(status_code=422, detail="that is not a delivery target")
    return clean


def _steps_say_unconfigured(steps: list[dict[str, Any]]) -> bool:
    """The CLI's convention, in one place: exactly one failed ``configuration``
    step means "this instance never set me up", not "this check failed"."""
    return len(steps) == 1 and steps[0]["step"] == UNCONFIGURED_STEP and not steps[0]["ok"]


async def verify_channel(name: str, target: str | None = None) -> dict[str, Any]:
    """Prove one channel works, step by step, and say what happened.

    Never reports a pass it did not observe. A channel that declares no
    ``verify`` returns an empty step list and ``verify_available: false`` —
    which is exactly what ``genus channel verify`` exits 2 for, and the
    opposite of the inert green check this repo has shipped before.
    """
    from fastapi import HTTPException

    from robothor.engine.channels import registry

    channel = registry.get_channel(name)
    if channel is None:
        raise HTTPException(status_code=404, detail="no channel by that name is registered")

    # Resolved as an attribute rather than called through the protocol:
    # ``verify`` is an OPTIONAL slot (``Channel`` declares it on nobody), and a
    # channel that does not have one is the ordinary case, not an error.
    prove = getattr(channel, "verify", None)
    if not callable(prove):
        report = await _health_report(name, channel)
        return {
            "channel": name,
            "configured": _configured(report),
            "verify_available": False,
            "error_class": None,
            "steps": [],
        }

    aimed = _checked_target(target)
    error_class: str | None = None
    try:
        raw = await asyncio.wait_for(prove(aimed), timeout=VERIFY_TIMEOUT_SECONDS)
        steps = [
            {"step": str(step), "ok": bool(ok), "detail": redact(str(detail))}
            for step, ok, detail in raw
        ]
    except TimeoutError:
        error_class = "TimeoutError"
        steps = [
            {
                "step": "verify",
                "ok": False,
                "detail": f"the channel did not finish within {VERIFY_TIMEOUT_SECONDS:.0f}s",
            }
        ]
    except Exception as exc:  # noqa: BLE001 — a broken channel is a report, not a traceback
        error_class = type(exc).__name__
        steps = [{"step": "verify", "ok": False, "detail": redact(f"{type(exc).__name__}: {exc}")}]

    return {
        "channel": name,
        # ``None`` — unknown — when the channel never answered. ``configured``
        # was derived by elimination ("not the unconfigured shape"), so a
        # channel that raised BECAUSE it has no credential came back
        # ``configured: true``: a pass nobody observed, which is the one thing
        # this module says it will never report. The listing already answers
        # unknown with null and the UI already renders it.
        "configured": None if error_class else not _steps_say_unconfigured(steps),
        "verify_available": True,
        # What went wrong, as a class name rather than a message: an operator
        # reading "unknown" needs to know whether the channel hung or threw.
        "error_class": error_class,
        "steps": steps,
    }


def register(app: FastAPI) -> None:
    """Mount the channel status routes on the engine app.

    Under ``/api/admin``, which ``engine/auth.py`` requires ``engine:control``
    for — the same gate the provider and scheduler surfaces sit behind.
    """
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get("/channels")
    async def list_channel_status() -> dict[str, Any]:
        """What a manifest could deliver to, and whether each one is set up."""
        return await channel_status()

    @router.post("/channels/{name}/verify")
    async def verify(name: str, body: VerifyRequest | None = None) -> dict[str, Any]:
        """Prove one channel works. Aimed by hand, so it may send a message."""
        return await verify_channel(name, (body or VerifyRequest()).target)

    app.include_router(router)
