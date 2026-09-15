"""Does this credential still work, and whose is it — without printing it.

Before this, the only way an assistant could show that a token it had been
handed was good was to USE it and report what happened, and the only way to
show WHICH token was stored was to print it. Both are wrong answers to a
question that comes up every time a credential is rotated.

A probe dials the vendor's cheapest identity endpoint and returns three things
and nothing else:

``ok``
    whether the credential authenticated.
``identity_hint``
    who it authenticated AS — a login, a team name, an account id. This is what
    makes a rotation checkable: a token that works but belongs to the wrong
    account is the failure a bare "ok" hides. It is the vendor's own public
    identifier for the account, never any part of the credential.
``error_class``
    ``auth``, ``rate_limit``, ``network``, ``unknown_kind``, ``not_configured``
    or ``unknown``. A CLASS, not a message: a vendor error body can quote the
    request it was sent, and a request carries an Authorization header.

No vendor message is ever returned, no status body, no exception text. The
class is the whole answer and it discloses nothing.

The kind is inferred from the vault key, not passed in, so the assistant cannot
ask for the GitHub probe to be run against an arbitrary value: the probe reads
the row itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["TestOutcome", "kind_for_key", "probe"]

#: How long any one probe may take. A credential test that hangs is a tool call
#: that hangs, and the assistant is usually mid-conversation with the operator
#: who just handed it the token.
TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class TestOutcome:
    """The entire result of a probe. Note what is not here: a value, a vendor
    message, a status body, a URL."""

    ok: bool
    identity_hint: str | None = None
    error_class: str | None = None


#: Vault key fragment -> probe kind. Matched against the SECOND path component
#: of a ``providers/<id>/api_key`` key and the second of a
#: ``channels/<name>/<field>`` key, which is the only place a vendor is named
#: in the naming convention (``robothor/vault/naming.py``).
_KINDS: dict[str, str] = {
    "github": "github",
    "openrouter": "openrouter",
    "openai": "openai",
    "anthropic": "anthropic",
    "brave": "brave",
    "slack": "slack",
    "twilio": "twilio",
    "cloudflare": "cloudflare",
    "jira": "jira",
}


#: Vendor spellings that mean the same vendor. ``gh_token`` is what an earlier
#: ``genus secrets migrate`` wrote for ``GH_TOKEN``, and ``kind_for_key`` did
#: not recognise it — so the migrated GitHub token was the one credential that
#: could not be ``vault_test``ed.
_KIND_ALIASES: dict[str, str] = {"gh": "github"}


def kind_for_key(vault_key: str) -> str | None:
    """Which probe, if any, knows how to test the row at ``vault_key``.

    Matched on whole ``/``- and ``_``-separated WORDS, not on substrings. The
    substring form claimed ``my_github_and_slack`` for GitHub, and would claim
    any key with a vendor's name buried in it — a probe aimed at the wrong
    vendor is a confident wrong answer, which is worse than ``unknown_kind``.

    ``None`` is a first-class answer: most credentials have no cheap identity
    endpoint, and claiming to have tested one that was never dialled is worse
    than saying so.
    """
    import re as _re

    words = [w for w in _re.split(r"[/_.-]+", str(vault_key).strip().lower()) if w]
    for word in words:
        resolved = _KIND_ALIASES.get(word, word)
        if resolved in _KINDS:
            return _KINDS[resolved]
    return None


def _classify(status: int) -> str:
    """A vendor's HTTP status, reduced to something safe to return."""
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "network"
    return "unknown"


#: Per kind: the request to make, and how to read an identity out of a 200.
#: ``auth`` is a function of the credential so a kind can put it in a header, a
#: query string or basic auth without the caller knowing which.
_PROBES: dict[str, dict[str, Any]] = {
    "github": {
        "url": "https://api.github.com/user",
        "auth": lambda value: {"Authorization": f"Bearer {value}"},
        "identity": lambda body: body.get("login"),
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/key",
        "auth": lambda value: {"Authorization": f"Bearer {value}"},
        "identity": lambda body: (body.get("data") or {}).get("label"),
    },
    "openai": {
        "url": "https://api.openai.com/v1/models",
        "auth": lambda value: {"Authorization": f"Bearer {value}"},
        "identity": lambda body: f"{len(body.get('data') or [])} models",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/models",
        "auth": lambda value: {"x-api-key": value, "anthropic-version": "2023-06-01"},
        "identity": lambda body: f"{len(body.get('data') or [])} models",
    },
    "brave": {
        "url": "https://api.search.brave.com/res/v1/web/search?q=ping",
        "auth": lambda value: {"X-Subscription-Token": value, "Accept": "application/json"},
        "identity": lambda body: "search ok" if body.get("web") else "reachable",
    },
    "slack": {
        "url": "https://slack.com/api/auth.test",
        "auth": lambda value: {"Authorization": f"Bearer {value}"},
        # Slack answers 200 with ok:false for a dead token, so the body is the
        # authority here and the status is not.
        "identity": lambda body: body.get("team") or body.get("user"),
        "body_ok": lambda body: bool(body.get("ok")),
    },
    "cloudflare": {
        "url": "https://api.cloudflare.com/client/v4/user/tokens/verify",
        "auth": lambda value: {"Authorization": f"Bearer {value}"},
        "identity": lambda body: (body.get("result") or {}).get("status"),
        "body_ok": lambda body: bool(body.get("success")),
    },
    "jira": {
        # The site is instance-specific, so the probe reads it from the same
        # settings the Jira tool dials. Filled in by :func:`probe`.
        "url": "",
        "auth": lambda value: {"Authorization": f"Basic {value}"},
        "identity": lambda body: body.get("displayName") or body.get("accountId"),
    },
    "twilio": {
        # Twilio's account SID is half the credential pair and is not a secret;
        # the probe needs it to build the URL, so it is read from settings.
        "url": "",
        "auth": lambda value: {"Authorization": f"Basic {value}"},
        "identity": lambda body: body.get("friendly_name") or body.get("status"),
    },
}


async def probe(kind: str, value: str) -> TestOutcome:
    """Dial ``kind``'s identity endpoint with ``value``.

    Never raises and never returns anything derived from the vendor's response
    body other than the identity hint its own ``identity`` reader extracts.
    """
    spec = _PROBES.get(kind)
    if spec is None:
        return TestOutcome(ok=False, error_class="unknown_kind")

    url = spec["url"]
    if not url:
        # A kind whose endpoint depends on instance configuration we do not
        # have. Saying "unknown_kind" would be a lie ("we have no probe"); this
        # says the probe exists and could not be aimed.
        return TestOutcome(ok=False, error_class="not_configured")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=spec["auth"](value))
    except Exception as exc:  # noqa: BLE001 - the class is the whole answer
        # The TYPE, never the text: an httpx error can carry the request it
        # was sent, and that request carries an Authorization header.
        logger.info("secrets: %s probe failed (%s)", kind, type(exc).__name__)
        return TestOutcome(ok=False, error_class="network")

    if response.status_code != 200:
        return TestOutcome(ok=False, error_class=_classify(response.status_code))

    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a 200 that is not JSON still authenticated
        return TestOutcome(ok=True, identity_hint=None)

    if not isinstance(body, dict):
        return TestOutcome(ok=True, identity_hint=None)

    body_ok = spec.get("body_ok")
    if body_ok is not None and not body_ok(body):
        # Slack and Cloudflare answer 200 with a failure flag. A probe that
        # read only the status would call a dead token good.
        return TestOutcome(ok=False, error_class="auth")

    try:
        hint = spec["identity"](body)
    except Exception:  # noqa: BLE001 - a missing field is not a failed test
        hint = None
    return TestOutcome(ok=True, identity_hint=str(hint) if hint else None)
